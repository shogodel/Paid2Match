"""Messages routes for Paid2Match.

Thread model (no Thread table — threads are derived from Message rows):
a thread is keyed by (other user, bounty_id, pitch_id). Same bounty /
pitch = same thread; both None = a direct thread.

URL scheme
----------
- GET  /messages/                          conversation list (threads)
- GET  /messages/thread/<other_user_id>    thread view (oldest-first)
     Query params carry the referral context:
       ?bounty_id=<id>&pitch_id=<id>
     Both absent = the direct thread with that user.
- POST /messages/thread/<other_user_id>    reply in-thread (same URL,
     same query params — the reply form posts back to the thread URL
     with the context in hidden fields too, so the context survives
     even if the query string is dropped).
- GET  /messages/<id>/read                 legacy flat URL — marks the
     message read, then 302s into the thread that contains it.
- GET  /messages/sent                      flat sent list (unchanged).
- POST /messages/<id>/reply                legacy reply (unchanged
     behaviour, now redirects back into the thread).
- GET  /messages/compose                   exact-email compose (unchanged).
- GET  /messages/api/conversations         JSON counterpart users
     (unchanged).

Read state: opening a thread marks all its incoming unread messages
read (is_read=True, read_at=now). A sent message shows "Seen" + the
read_at time once the recipient has opened the thread. The thread
POST and the delete endpoint answer AJAX (fetch) callers with JSON;
the updates endpoint lets the thread view poll for new messages.
"""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_babel import gettext as _
from flask_login import login_required, current_user
from sqlalchemy import or_, and_, desc

from extensions import db, get_or_404
from models import utc_now
from models.message import Message
from models.moderation import Block
from models.user import User
from models.bounty import Bounty
from models.pitch import Pitch
from models.profile import Profile
from utils.notifications import notify

messages_bp = Blueprint('messages', __name__)


def _wants_json():
    """True when the client asked for JSON (fetch with an explicit
    Accept or X-Requested-With header). Plain form posts get the
    classic redirect + flash behaviour. Same rule as the social
    blueprint's _wants_json."""
    return (
        request.headers.get('X-Requested-With') == 'XMLHttpRequest'
        or request.accept_mimetypes.best == 'application/json'
    )


def _message_json(message):
    """JSON payload for one message: the message dict (id, content,
    created_at, sender info, read_at) plus the rendered bubble, so
    the client can append it without a reload."""
    return {
        'ok': True,
        'message': message.to_dict(),
        'html': render_template('messages/_message.html', msg=message),
    }


def _mark_read(messages):
    """Mark incoming unread messages read: is_read stays the flag,
    read_at records when the recipient saw them (same UTC clock the
    app uses everywhere)."""
    now = utc_now()
    changed = False
    for m in messages:
        if m.recipient_id == current_user.id and not m.is_read:
            m.is_read = True
            m.read_at = now
            changed = True
    if changed:
        db.session.commit()


def _thread_messages(other_id, bounty_id, pitch_id):
    """All messages in one thread, oldest first."""
    return Message.query.filter(
        or_(
            and_(
                Message.sender_id == current_user.id,
                Message.recipient_id == other_id,
            ),
            and_(
                Message.sender_id == other_id,
                Message.recipient_id == current_user.id,
            ),
        ),
        Message.bounty_id == bounty_id,
        Message.pitch_id == pitch_id,
    ).order_by(Message.created_at).all()


# ---------------------------------------------------------------- helpers


def _thread_key(message, user_id):
    """(other user, bounty_id, pitch_id) for a message involving user_id."""
    other_id = (
        message.recipient_id if message.sender_id == user_id
        else message.sender_id
    )
    return (other_id, message.bounty_id, message.pitch_id)


def _thread_url(other_user_id, bounty_id=None, pitch_id=None):
    """URL of a thread. None context values are dropped from the
    query string by url_for, so a direct thread is just
    /messages/thread/<other_user_id>."""
    return url_for(
        'messages.thread', other_user_id=other_user_id,
        bounty_id=bounty_id, pitch_id=pitch_id,
    )


def _thread_url_for_message(message, user_id):
    """URL of the thread containing ``message``, seen from ``user_id``."""
    other_id, bounty_id, pitch_id = _thread_key(message, user_id)
    return _thread_url(other_id, bounty_id=bounty_id, pitch_id=pitch_id)


def _context_ids():
    """bounty_id / pitch_id for the current thread request. POSTs carry
    them as hidden fields as well as in the query string, so read
    request.values (args + form) and treat '' as None."""
    bounty_id = request.values.get('bounty_id') or None
    pitch_id = request.values.get('pitch_id') or None
    return bounty_id, pitch_id


def _validate_context(bounty_id, pitch_id):
    """A thread's context must point at real rows, otherwise a reply
    would carry a dangling FK. Returns the Bounty (or None)."""
    bounty = None
    if bounty_id:
        bounty = get_or_404(Bounty, bounty_id)
    if pitch_id:
        pitch = get_or_404(Pitch, pitch_id)
        if bounty is None:
            bounty = pitch.bounty
    return bounty


def _group_threads(user_id):
    """All messages involving user_id, grouped into threads. Returns a
    list of dicts, newest thread first (by last-message time)."""
    messages = Message.query.filter(
        or_(Message.sender_id == user_id, Message.recipient_id == user_id)
    ).order_by(Message.created_at).all()

    threads = {}
    for m in messages:
        key = _thread_key(m, user_id)
        thread = threads.get(key)
        if thread is None:
            thread = threads[key] = {
                'other_user_id': key[0],
                'bounty_id': key[1],
                'pitch_id': key[2],
                'messages': [],
                'unread': 0,
            }
        thread['messages'].append(m)
        if m.recipient_id == user_id and not m.is_read:
            thread['unread'] += 1

    result = []
    for thread in threads.values():
        thread['last_message'] = thread['messages'][-1]
        result.append(thread)
    # Newest activity first; created_at ties (same-second sends) fall
    # back to insertion order, which order_by(created_at) already gave.
    result.sort(key=lambda t: t['last_message'].created_at, reverse=True)
    return result


def _enrich_threads(threads):
    """Attach the other user, their profile, the thread URL and the
    bounty title (for the referral-context chip) to each thread."""
    other_ids = [t['other_user_id'] for t in threads]
    users = {
        u.id: u for u in
        User.query.filter(User.id.in_(other_ids)).all()
    } if other_ids else {}
    profiles = {
        p.user_id: p for p in
        Profile.query.filter(Profile.user_id.in_(other_ids)).all()
    } if other_ids else {}
    bounty_ids = {t['bounty_id'] for t in threads if t['bounty_id']}
    bounties = {
        b.id: b for b in
        Bounty.query.filter(Bounty.id.in_(bounty_ids)).all()
    } if bounty_ids else {}
    pitch_ids = {t['pitch_id'] for t in threads if t['pitch_id']}
    pitches = {
        p.id: p for p in
        Pitch.query.filter(Pitch.id.in_(pitch_ids)).all()
    } if pitch_ids else {}
    for t in threads:
        t['other_user'] = users.get(t['other_user_id'])
        t['other_profile'] = profiles.get(t['other_user_id'])
        t['url'] = _thread_url(
            t['other_user_id'], bounty_id=t['bounty_id'],
            pitch_id=t['pitch_id'],
        )
        bounty = bounties.get(t['bounty_id'])
        if bounty is None and t['pitch_id']:
            pitch = pitches.get(t['pitch_id'])
            bounty = bounties.get(pitch.bounty_id) if pitch else None
            if bounty is None and pitch is not None:
                bounty = pitch.bounty
        t['bounty'] = bounty
    return threads


# ---------------------------------------------------------------- inbox


@messages_bp.route('/')
@login_required
def inbox():
    """GET /messages - Conversation list, grouped into threads.

    ?q=<text> filters the list server-side: a thread matches if any
    message in it contains the text (case-insensitive) or the other
    user's name / handle matches."""
    threads = _enrich_threads(_group_threads(current_user.id))
    q = (request.args.get('q') or '').strip()
    if q:
        needle = q.lower()
        kept = []
        for t in threads:
            other = t.get('other_user')
            name_hit = bool(other) and (
                needle in (other.full_name or '').lower()
                or needle in (other.handle or '').lower()
            )
            content_hit = any(
                needle in (m.content or '').lower()
                for m in t['messages']
            )
            if name_hit or content_hit:
                kept.append(t)
        threads = kept
    return render_template('messages/inbox.html', threads=threads, q=q)


# ---------------------------------------------------------------- thread


@messages_bp.route('/thread/<other_user_id>', methods=['GET', 'POST'])
@login_required
def thread(other_user_id):
    """GET/POST /messages/thread/<other_user_id>[?bounty_id=&pitch_id=]
    — the full conversation with one user in one context, oldest
    first, with a reply box. POST sends the reply in-thread (keeps
    bounty/pitch context, block check kept). Opening the thread marks
    all its incoming unread messages read."""
    other = get_or_404(User, other_user_id)
    bounty_id, pitch_id = _context_ids()
    bounty = _validate_context(bounty_id, pitch_id)

    if request.method == 'POST':
        content = (request.form.get('content') or '').strip()
        if not content:
            if _wants_json():
                return jsonify(
                    {'ok': False, 'error': _('Write a message first.')}
                ), 400
            flash(_('Write a message first.'), 'warning')
            return redirect(_thread_url(other.id, bounty_id, pitch_id))

        # A block means "they cannot follow or message you" — the
        # follow route enforces it; messaging must too. The check
        # applies to the AJAX send exactly as to the form send.
        if Block.query.filter_by(
            blocker_id=other.id, blocked_id=current_user.id,
            kind='block',
        ).first():
            if _wants_json():
                return jsonify(
                    {'ok': False, 'error': _('You cannot message this user.')}
                ), 403
            flash(_('You cannot message this user.'), 'danger')
            return redirect(_thread_url(other.id, bounty_id, pitch_id))

        reply_msg = Message(
            sender_id=current_user.id,
            recipient_id=other.id,
            bounty_id=bounty_id,
            pitch_id=pitch_id,
            content=content
        )
        db.session.add(reply_msg)
        db.session.commit()

        # M1: notify the other party (in-app + email — a key event).
        # The link is the legacy read URL, which 302s into the thread.
        notify(
            other.id, 'message_new',
            f'{current_user.full_name} replied to your message',
            (content or '')[:200],
            actor_id=current_user.id,
            link_url=url_for('messages.read', message_id=reply_msg.id),
        )
        db.session.commit()

        if _wants_json():
            # AJAX send: no reload — the client appends the bubble.
            return jsonify(_message_json(reply_msg))

        flash(_('Reply sent!'), 'success')
        return redirect(_thread_url(other.id, bounty_id, pitch_id))

    messages = _thread_messages(other.id, bounty_id, pitch_id)

    # Opening the thread marks its incoming unread messages read
    # (is_read + read_at).
    _mark_read(messages)

    other_profile = Profile.query.filter_by(user_id=other.id).first()
    return render_template(
        'messages/thread.html',
        other_user=other, other_profile=other_profile,
        messages=messages, bounty=bounty,
        bounty_id=bounty_id, pitch_id=pitch_id,
    )


@messages_bp.route('/thread/<other_user_id>/updates')
@login_required
def thread_updates(other_user_id):
    """GET /messages/thread/<other_user_id>/updates?after=<message_id>
    — messages in that thread newer than ``after`` (same shape as
    the AJAX send response, one item per message, including the
    rendered bubble). The thread context rides in the query string
    (?bounty_id=&pitch_id=), as on the thread view. Picking messages
    up by polling marks incoming ones read — the user is looking at
    the thread."""
    other = get_or_404(User, other_user_id)
    bounty_id, pitch_id = _context_ids()
    _validate_context(bounty_id, pitch_id)

    messages = _thread_messages(other.id, bounty_id, pitch_id)
    after_id = request.args.get('after') or None
    if after_id:
        # Everything after the anchor message in oldest-first order
        # (same-second sends share a timestamp, so position in the
        # ordered list — not the clock — decides "newer"). An unknown
        # anchor (e.g. deleted) falls back to the whole thread; the
        # client dedupes by message id.
        idx = next(
            (i for i, m in enumerate(messages) if m.id == after_id),
            None,
        )
        new = messages[idx + 1:] if idx is not None else messages
    else:
        new = messages

    _mark_read(new)

    return jsonify({
        'ok': True,
        'messages': [_message_json(m) for m in new],
    })


@messages_bp.route('/<message_id>/delete', methods=['POST'])
@login_required
def delete_message(message_id):
    """POST /messages/<id>/delete — hard-delete your OWN sent
    message. Anyone else's message is refused (403 for AJAX, a
    redirect + flash otherwise) and left untouched. AJAX gets
    {"ok": true}; a plain form post redirects back to the thread.
    Note: notification links pointing at a deleted message's legacy
    read URL will no longer resolve (acceptable — the message is
    gone). CSRF is enforced app-wide by CSRFProtect on every POST."""
    message = get_or_404(Message, message_id)

    if message.sender_id != current_user.id:
        if _wants_json():
            return jsonify(
                {'ok': False, 'error': _('You cannot delete this message.')}
            ), 403
        flash(_('You cannot delete this message.'), 'danger')
        return redirect(url_for('messages.inbox'))

    thread_url = _thread_url_for_message(message, current_user.id)
    db.session.delete(message)
    db.session.commit()

    if _wants_json():
        return jsonify({'ok': True})

    flash(_('Message deleted.'), 'success')
    return redirect(thread_url)


@messages_bp.route('/sent')
@login_required
def sent():
    """GET /messages/sent - User's sent messages."""
    messages = Message.query.filter(
        Message.sender_id == current_user.id
    ).order_by(desc(Message.created_at)).all()

    return render_template('messages/sent.html', messages=messages)


@messages_bp.route('/compose', methods=['GET', 'POST'])
@login_required
def compose():
    """GET/POST /messages/compose - Compose new message."""
    if request.method == 'POST':
        recipient_email = request.form.get('recipient_email')
        content = (request.form.get('content') or '').strip()

        recipient = User.query.filter_by(email=recipient_email).first()
        if not recipient:
            flash(_('User not found'), 'danger')
            return redirect(url_for('messages.compose'))

        if not content:
            # Message.content is NOT NULL — an empty send used to
            # crash with an IntegrityError (500).
            flash(_('Write a message first.'), 'warning')
            return redirect(url_for('messages.compose'))

        # A block means "they cannot follow or message you"
        # (models/moderation.py) — the follow route enforces it;
        # messaging must too.
        if Block.query.filter_by(
            blocker_id=recipient.id, blocked_id=current_user.id,
            kind='block',
        ).first():
            flash(_('You cannot message this user.'), 'danger')
            return redirect(url_for('messages.compose'))

        message = Message(
            sender_id=current_user.id,
            recipient_id=recipient.id,
            content=content
        )
        db.session.add(message)
        db.session.commit()

        # M1: notify the recipient (in-app + email — a key event).
        notify(
            recipient.id, 'message_new',
            f'New message from {current_user.full_name}',
            (content or '')[:200],
            actor_id=current_user.id,
            link_url=url_for('messages.read', message_id=message.id),
        )
        db.session.commit()

        flash(_('Message sent!'), 'success')
        return redirect(url_for('messages.sent'))

    return render_template('messages/compose.html')


@messages_bp.route('/<message_id>/read')
@login_required
def read(message_id):
    """GET /messages/<id>/read - Legacy flat URL. Marks the message
    read (as before) and 302s into the thread that contains it."""
    message = get_or_404(Message, message_id)

    if message.recipient_id != current_user.id and message.sender_id != current_user.id:
        flash(_('Access denied'), 'danger')
        return redirect(url_for('messages.inbox'))

    if message.recipient_id == current_user.id and not message.is_read:
        message.is_read = True
        message.read_at = utc_now()
        db.session.commit()

    return redirect(_thread_url_for_message(message, current_user.id))


@messages_bp.route('/<message_id>/reply', methods=['POST'])
@login_required
def reply(message_id):
    """POST /messages/<id>/reply - Reply to a message."""
    original = get_or_404(Message, message_id)

    if original.recipient_id != current_user.id:
        flash(_('Access denied'), 'danger')
        return redirect(url_for('messages.inbox'))

    content = (request.form.get('content') or '').strip()
    if not content:
        flash(_('Write a message first.'), 'warning')
        return redirect(url_for('messages.inbox'))

    if Block.query.filter_by(
        blocker_id=original.sender_id, blocked_id=current_user.id,
        kind='block',
    ).first():
        flash(_('You cannot message this user.'), 'danger')
        return redirect(url_for('messages.inbox'))

    # Phase 3: a reply stays in the thread — the referral context
    # (bounty + pitch) is copied from the original message, so a
    # referral conversation answered from the inbox still shows on
    # the referral detail page.
    reply_msg = Message(
        sender_id=current_user.id,
        recipient_id=original.sender_id,
        bounty_id=original.bounty_id,
        pitch_id=original.pitch_id,
        content=content
    )
    db.session.add(reply_msg)
    db.session.commit()

    # M1: notify the original sender (in-app + email).
    notify(
        original.sender_id, 'message_new',
        f'{current_user.full_name} replied to your message',
        (content or '')[:200],
        actor_id=current_user.id,
        link_url=url_for('messages.read', message_id=reply_msg.id),
    )
    db.session.commit()

    flash(_('Reply sent!'), 'success')
    return redirect(_thread_url_for_message(reply_msg, current_user.id))


@messages_bp.route('/api/users')
@login_required
def api_users():
    """GET /messages/api/users?q=<text> - JSON user search for the
    compose picker. Matches name, handle or email (case-insensitive);
    2+ chars required (shorter queries return []). Max 10 results.
    Excludes the current user and any user who blocked them."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify([])

    blocked_by = db.session.query(Block.blocker_id).filter(
        Block.blocked_id == current_user.id,
        Block.kind == 'block',
    )
    like = f'%{q}%'
    users = User.query.filter(
        User.id != current_user.id,
        User.id.notin_(blocked_by),
        or_(
            User.full_name.ilike(like),
            User.handle.ilike(like),
            User.email.ilike(like),
        ),
    ).order_by(User.full_name).limit(10).all()

    return jsonify([{
        'id': u.id,
        'full_name': u.full_name,
        'handle': u.handle,
        'email': u.email,
    } for u in users])


@messages_bp.route('/api/conversations')
@login_required
def api_conversations():
    """GET /messages/api/conversations - JSON API for conversations."""
    sent = db.session.query(Message.recipient_id).filter(
        Message.sender_id == current_user.id
    ).distinct().all()

    received = db.session.query(Message.sender_id).filter(
        Message.recipient_id == current_user.id
    ).distinct().all()

    user_ids = set([s[0] for s in sent] + [r[0] for r in received])

    users = User.query.filter(User.id.in_(user_ids)).all()

    return jsonify([{
        'id': u.id,
        'name': u.full_name,
        'email': u.email
    } for u in users])
