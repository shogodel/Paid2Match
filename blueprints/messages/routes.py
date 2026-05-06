"""Messages routes for Paid2Match."""
from flask import Blueprint, render_template, request, redirect, url_for, flash, jsonify
from flask_login import login_required, current_user
from sqlalchemy import or_, desc

from extensions import db, get_or_404
from models.message import Message
from models.user import User

messages_bp = Blueprint('messages', __name__)


@messages_bp.route('/')
@login_required
def inbox():
    """GET /messages - User's inbox."""
    messages = Message.query.filter(
        Message.recipient_id == current_user.id
    ).order_by(desc(Message.created_at)).all()
    
    return render_template('messages/inbox.html', messages=messages)


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
        content = request.form.get('content')
        
        recipient = User.query.filter_by(email=recipient_email).first()
        if not recipient:
            flash('User not found', 'danger')
            return redirect(url_for('messages.compose'))
        
        message = Message(
            sender_id=current_user.id,
            recipient_id=recipient.id,
            content=content
        )
        db.session.add(message)
        db.session.commit()
        
        flash('Message sent!', 'success')
        return redirect(url_for('messages.sent'))
    
    return render_template('messages/compose.html')


@messages_bp.route('/<message_id>/read')
@login_required
def read(message_id):
    """GET /messages/<id>/read - Read a message."""
    message = get_or_404(Message, message_id)
    
    if message.recipient_id != current_user.id and message.sender_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('messages.inbox'))
    
    if message.recipient_id == current_user.id and not message.is_read:
        message.is_read = True
        db.session.commit()
    
    return render_template('messages/read.html', message=message)


@messages_bp.route('/<message_id>/reply', methods=['POST'])
@login_required
def reply(message_id):
    """POST /messages/<id>/reply - Reply to a message."""
    original = get_or_404(Message, message_id)
    
    if original.recipient_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('messages.inbox'))
    
    content = request.form.get('content')
    
    reply_msg = Message(
        sender_id=current_user.id,
        recipient_id=original.sender_id,
        content=content
    )
    db.session.add(reply_msg)
    db.session.commit()
    
    flash('Reply sent!', 'success')
    return redirect(url_for('messages.inbox'))


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