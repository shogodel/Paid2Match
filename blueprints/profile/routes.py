"""Profile routes for Paid2Match."""
import os
from flask import Blueprint, render_template, redirect, url_for, flash, request
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename
from extensions import db, get_or_404
from models.user import User
from models.profile import Profile
from models.bounty import Bounty
from models.pitch import Pitch
from models.match import Match
from models.dispute import Dispute
from .forms import ProfileForm, SetupForm

profile_bp = Blueprint('profile', __name__)

ALLOWED_EXTENSIONS = {'png', 'jpg', 'jpeg', 'gif'}
UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'uploads', 'avatars')

def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def save_avatar(file):
    if file and allowed_file(file.filename):
        filename = secure_filename(f"{current_user.id}_{file.filename}")
        os.makedirs(UPLOAD_FOLDER, exist_ok=True)
        filepath = os.path.join(UPLOAD_FOLDER, filename)
        file.save(filepath)
        return f"/uploads/avatars/{filename}"
    return None


@profile_bp.route('/setup', methods=['GET', 'POST'])
@login_required
def setup():
    existing = Profile.query.filter_by(user_id=current_user.id).first()
    if existing:
        return redirect(url_for('profile.view'))
    
    form = SetupForm()
    if form.validate_on_submit():
        profile = Profile(
            user_id=current_user.id,
            profile_type=form.profile_type.data,
            display_name=form.display_name.data or current_user.full_name,
            headline=form.headline.data,
            bio=form.bio.data,
            location=form.location.data,
            company_name=form.company_name.data,
            skills=form.skills.data,
            website=form.website.data,
            linkedin_url=form.linkedin_url.data,
            github_url=form.github_url.data,
            twitter_handle=form.twitter_handle.data or None,
            remote_ok=form.remote_ok.data or False,
            relocation_assistance=form.relocation_assistance.data,
            privacy_level=0
        )
        
        # Handle avatar upload
        if form.avatar.data:
            avatar_url = save_avatar(form.avatar.data)
            if avatar_url:
                profile.avatar_url = avatar_url
        
        db.session.add(profile)
        db.session.commit()
        flash('Profile created!', 'success')
        return redirect(url_for('bounties.board'))
    return render_template('profile/setup.html', form=form)


@profile_bp.route('/edit', methods=['GET', 'POST'])
@login_required
def edit():
    profile = Profile.query.filter_by(user_id=current_user.id).first()
    if not profile:
        return redirect(url_for('profile.setup'))
    
    form = ProfileForm(obj=profile)
    if form.validate_on_submit():
        profile.display_name = form.display_name.data
        profile.headline = form.headline.data
        profile.bio = form.bio.data
        profile.location = form.location.data
        profile.company_name = form.company_name.data
        profile.skills = form.skills.data
        profile.website = form.website.data
        profile.linkedin_url = form.linkedin_url.data
        profile.github_url = form.github_url.data
        profile.twitter_handle = form.twitter_handle.data
        profile.remote_ok = form.remote_ok.data
        profile.relocation_assistance = form.relocation_assistance.data
        profile.privacy_level = form.privacy_level.data
        profile.profile_type = form.profile_type.data
        
        # Handle avatar upload
        if form.avatar.data:
            avatar_url = save_avatar(form.avatar.data)
            if avatar_url:
                profile.avatar_url = avatar_url
        
        db.session.commit()
        flash('Profile updated!', 'success')
        return redirect(url_for('profile.view'))
    return render_template('profile/edit.html', form=form)


@profile_bp.route('/')
@login_required
def view():
    profile = Profile.query.filter_by(user_id=current_user.id).first()
    if not profile:
        return redirect(url_for('profile.setup'))
    
    pitches_count = Pitch.query.filter_by(hunter_id=current_user.id).count()
    matches_count = Match.query.filter_by(hunter_id=current_user.id).count()
    disputes_count = Dispute.query.filter_by(raised_by_id=current_user.id).count()
    reputation = profile.reputation_score or (pitches_count * 1) + (matches_count * 10) - (disputes_count * 5)
    
    return render_template('profile/view.html',
                        profile=profile,
                        pitches_count=pitches_count,
                        matches_count=matches_count,
                        disputes_count=disputes_count,
                        reputation=reputation)


@profile_bp.route('/<string:user_id>')
def public_profile(user_id):
    user = get_or_404(User, user_id)
    profile = Profile.query.filter_by(user_id=user_id).first()
    
    # If profile doesn't exist or is private
    if not profile or profile.privacy_level == 2:
        flash('This profile is private or does not exist', 'warning')
        return redirect(url_for('index'))
    
    pitches_count = Pitch.query.filter_by(hunter_id=user_id).count()
    matches_count = Match.query.filter_by(hunter_id=user_id).count()
    disputes_count = Dispute.query.filter_by(raised_by_id=user_id).count()
    reputation = profile.reputation_score or (pitches_count * 1) + (matches_count * 10) - (disputes_count * 5)
    
    return render_template('profile/public.html',
                        user=user, profile=profile,
                        pitches_count=pitches_count,
                        matches_count=matches_count,
                        disputes_count=disputes_count,
                        reputation=reputation)
