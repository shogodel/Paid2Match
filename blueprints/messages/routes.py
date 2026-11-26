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
from flask import (
    Blueprint, g, render_template, request, redirect, url_for, flash,
    jsonify, abort,
)
from flask_babel import gettext as _
from flask_login import login_required, current_user
from sqlalchemy import or_, and_, desc, func
from sqlalchemy.orm import selectinload

from extensions import db, get_or_404
from models import utc_now
from models.message import MAX_MESSAGE_LENGTH, Message
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


def _mark_read(other_id, bounty_id, pitch_id):
    """Mark incoming unread messages in this thread read: is_read
    stays the flag, read_at records when the recipient saw them
    (same UTC clock the app uses everywhere).

    Audit 2026-11-25: the old loop set the flag on each message and
    let the commit flush one UPDATE per unread message (a 15-message
    thread opened with 8 unread = 8 UPDATEs). One bulk UPDATE
    replaces them. It runs BEFORE the thread's messages are loaded:
    the commit expires every object in the session, so marking read
    after loading made each message (and its sender) reload one by
    one when the bubbles rendered — 10 extra SELECTs on a 10-message
    thread. synchronize_session=False: 'evaluate' would reload every
    expired message object in the session one by one (worse than the
    UPDATEs it replaces)."""
    now = utc_now()
    unread_ids = [
        m_id for (m_id,) in db.session.query(Message.id).filter(
            Message.sender_id == other_id,
            Message.recipient_id == current_user.id,
            Message.is_read == False,  # noqa: E712
            Message.bounty_id == bounty_id,
            Message.pitch_id == pitch_id,
        ).all()
    ]
    if not unread_ids:
        return
    Message.query.filter(Message.id.in_(unread_ids)).update(
        {'is_read': True, 'read_at': now},
        synchronize_session=False,
    )
    db.session.commit()
    # The navbar badge (inject_notifications, app.py) caches the
    # unread counts on flask.g for the request. The badge renders
    # later in THIS request, so drop the cached counts — otherwise
    # the badge still shows the messages just marked read. In
    # production g is fresh per request and this is a no-op; in tests
    # the app context is held across requests, so without this the
    # stale count leaks into the next request too.
    g.pop('_notifications_ctx', None)


def _thread_messages(other_id, bounty_id, pitch_id):
    """All messages in one thread, oldest first."""
    # Audit 2026-11-25: the bubble template reads msg.sender per
    # message — eager-load both sides (1 query, not 1 per message).
    return Message.query.options(
        selectinload(Message.sender), selectinload(Message.recipient),
    ).filter(
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


def _validate_content(content):
    """Empty / over-long guard shared by every send path. Returns the
    (translated) error string, or None when the content is fine."""
    if not content:
        return _('Write a message first.')
    if len(content) > MAX_MESSAGE_LENGTH:
        return _(
            'Message is too long (maximum %(max)s characters).',
            max=MAX_MESSAGE_LENGTH,
        )
    return None


def _validate_context(bounty_id, pitch_id):
    """A thread's context must point at real rows, otherwise a reply
    would carry a dangling FK. Returns (bounty, bounty_id, pitch_id,
    pitch) with the context NORMALIZED:

    - pitch-only (?pitch_id=, no bounty_id): the thread key must use
      the pitch's bounty, or the reply forks a second thread keyed
      (other, None, pitch) apart from the referral thread.
    - bounty + pitch that disagree (pitch belongs to another bounty):
      contradictory context — 404 rather than minting an orphan
      thread no real conversation matches.
    """
    bounty = None
    pitch = None
    if pitch_id:
        pitch = get_or_404(Pitch, pitch_id)
        if bounty_id and pitch.bounty_id != bounty_id:
            abort(404)
        bounty_id = pitch.bounty_id
        bounty = pitch.bounty
        if bounty is None:
            bounty = get_or_404(Bounty, bounty_id)
    elif bounty_id:
        bounty = get_or_404(Bounty, bounty_id)
    return bounty, bounty_id, pitch_id, pitch


def _check_thread_participation(other, bounty, pitch):
    """True when the (sender, recipient) pair may carry this thread
    context. A pitch-tagged message lands on the referral detail
    page for that pitch — visible to the referrer, the poster and
    the candidate — so only the pitch's hunter and the need's poster
    may send one, to each other. Without a pitch, a bounty-tagged
    message still needs both parties to be the poster or a hunter
    on that need; anyone else tagging a bounty/pitch id onto a DM
    would be writing into a negotiation they are not part of."""
    if pitch is not None:
        if bounty is None:
            return False
        return {current_user.id, other.id} == {
            pitch.hunter_id, bounty.poster_id,
        }
    if bounty is not None:
        parties = {bounty.poster_id}
        parties.update(
            p.hunter_id for p in
            Pitch.query.filter_by(bounty_id=bounty.id).all()
        )
        return current_user.id in parties and other.id in parties
    return True


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
    bounty, bounty_id, pitch_id, pitch = _validate_context(
        bounty_id, pitch_id)

    if request.method == 'POST':
        if not _check_thread_participation(other, bounty, pitch):
            if _wants_json():
                return jsonify(
                    {'ok': False,
                     'error': _('You cannot message about this referral.')}
                ), 403
            flash(_('You cannot message about this referral.'), 'danger')
            return redirect(_thread_url(other.id, bounty_id, pitch_id))
        content = (request.form.get('content') or '').strip()
        content_error = _validate_content(content)
        if content_error:
            if _wants_json():
                return jsonify(
                    {'ok': False, 'error': content_error}
                ), 400
            flash(content_error, 'warning')
            return redirect(_thread_url(other.id, bounty_id, pitch_id))

        if other.id == current_user.id:
            # No self-DMs: the picker already excludes you, and a
            # message to yourself would inflate your own unread
            # badge with a message you wrote.
            if _wants_json():
                return jsonify(
                    {'ok': False,
                     'error': _('You cannot message yourself.')}
                ), 400
            flash(_('You cannot message yourself.'), 'warning')
            return redirect(_thread_url(other.id, bounty_id, pitch_id))

        if other.is_deleted:
            # History with a deleted account stays readable (GET),
            # but no NEW messages go to an account that is gone.
            if _wants_json():
                return jsonify(
                    {'ok': False,
                     'error': _('You cannot message this user.')}
                ), 400
            flash(_('You cannot message this user.'), 'danger')
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
            _('%(name)s replied to your message', name=current_user.full_name),
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

    # Opening the thread marks its incoming unread messages read
    # (is_read + read_at) — before the messages are loaded, so the
    # commit in _mark_read cannot expire them (see its docstring).
    _mark_read(other.id, bounty_id, pitch_id)

    messages = _thread_messages(other.id, bounty_id, pitch_id)

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
    _bounty, bounty_id, pitch_id, _pitch = _validate_context(
        bounty_id, pitch_id)

    # Mark-read first (before any message is loaded — the commit in
    # _mark_read expires the session; see its docstring).
    _mark_read(other.id, bounty_id, pitch_id)

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
    # Audit 2026-11-25: the sent list reads msg.recipient per row —
    # eager-load it (1 query, not 1 per message).
    messages = Message.query.options(
        selectinload(Message.recipient),
    ).filter(
        Message.sender_id == current_user.id
    ).order_by(desc(Message.created_at)).all()

    return render_template('messages/sent.html', messages=messages)


@messages_bp.route('/compose', methods=['GET', 'POST'])
@login_required
def compose():
    """GET/POST /messages/compose - Compose new message."""
    if request.method == 'POST':
        recipient_email = (request.form.get('recipient_email') or '').strip()
        content = (request.form.get('content') or '').strip()

        # Email lookup is case-insensitive (like the OAuth callback):
        # 'Remus@Example.com' is the same account as
        # 'remus@example.com', and the exact-match lookup used to
        # answer 'User not found' for it.
        recipient = User.query.filter(
            func.lower(User.email) == recipient_email.lower()
        ).first() if recipient_email else None
        if not recipient or recipient.is_deleted:
            flash(_('User not found'), 'danger')
            return redirect(url_for('messages.compose'))

        content_error = _validate_content(content)
        if content_error:
            # Message.content is NOT NULL — an empty send used to
            # crash with an IntegrityError (500).
            flash(content_error, 'warning')
            return redirect(url_for('messages.compose'))

        if recipient.id == current_user.id:
            flash(_('You cannot message yourself.'), 'warning')
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
            _('New message from %(name)s', name=current_user.full_name),
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
    content_error = _validate_content(content)
    if content_error:
        flash(content_error, 'warning')
        return redirect(url_for('messages.inbox'))

    sender = db.session.get(User, original.sender_id)
    if sender is None or sender.is_deleted or sender.id == current_user.id:
        # No reply goes to a deleted account — or to yourself.
        flash(_('You cannot message this user.'), 'danger')
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
        _('%(name)s replied to your message', name=current_user.full_name),
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
    Excludes the current user, any user who blocked them, and
    deleted/deactivated accounts (their rows keep a placeholder
    email and must not be messageable). The response carries NO
    email address: the picker only needs a name and a handle to
    open the thread, and returning emails would let any logged-in
    user harvest every user's address with two-letter queries
    (searching *by* an email you already know still works — the
    match happens server-side)."""
    q = (request.args.get('q') or '').strip()
    if len(q) < 2:
        return jsonify([])

    blocked_by = db.session.query(Block.blocker_id).filter(
        Block.blocked_id == current_user.id,
        Block.kind == 'block',
    )
    # Escape LIKE wildcards in the query: an unescaped '%%' matched
    # every user in the directory (and '_' matched any character),
    # turning the picker into a user-enumeration endpoint that also
    # returns emails. is_deleted is NULL for pre-soft-delete rows
    # (the migration added the column without a backfill), so the
    # filter must be IS NOT TRUE, not = False.
    escaped_q = (
        q.replace('\\', '\\\\').replace('%', '\\%').replace('_', '\\_')
    )
    like = f'%{escaped_q}%'
    users = User.query.filter(
        User.id != current_user.id,
        User.is_deleted.isnot(True),
        User.id.notin_(blocked_by),
        User.is_active == True,  # noqa: E712
        User.is_deleted == False,  # noqa: E712
        or_(
            User.full_name.ilike(like, escape='\\'),
            User.handle.ilike(like, escape='\\'),
            User.email.ilike(like, escape='\\'),
        ),
    ).order_by(User.full_name).limit(10).all()

    return jsonify([{
        'id': u.id,
        'full_name': u.full_name,
        'handle': u.handle,
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
