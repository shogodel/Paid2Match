"""Profile routes for Paid2Match."""
import os
from flask import Blueprint, render_template, redirect, url_for, flash, request, session
from flask_login import login_required, current_user
from sqlalchemy import desc
from werkzeug.utils import secure_filename
from extensions import db, get_or_404
from models.user import User
from models.profile import Profile
from models.bounty import Bounty
from models.follow import Follow
from models.pitch import Pitch
from models.match import Match
from models.dispute import Dispute
from models.post import Post
from models.endorsement import Pledge
from models.linked_account import (
    OAUTH_PLATFORMS,
    PLATFORMS,
    LinkedAccount,
)
from models.profile_view import ProfileView
from models.review import revealed_reviews_for_user
from models.verification import VerificationRequest
from utils.analytics import get_analytics
from utils.endorsements import endorsements_for, give_endorsement
from utils.linked_accounts import (
    exchange_oauth_code,
    fetch_oauth_profile,
    get_or_create_link,
    mark_verified,
    oauth_authorize_url,
    oauth_available,
    oauth_state_token,
    refresh_linked_account_expiry,
    verified_linked_count,
    verify_bio_link,
    verify_code,
    visible_linked_accounts,
)
from utils.sla import sla_breaches
from utils.trust import ratings_summary, trust_level
from .forms import EndorseForm, LinkedAccountForm, ProfileForm, SetupForm

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
            instagram_url=form.instagram_url.data,
            facebook_url=form.facebook_url.data,
            tiktok_url=form.tiktok_url.data,
            youtube_url=form.youtube_url.data,
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
        profile.instagram_url = form.instagram_url.data
        profile.facebook_url = form.facebook_url.data
        profile.tiktok_url = form.tiktok_url.data
        profile.youtube_url = form.youtube_url.data
        profile.twitter_handle = form.twitter_handle.data
        profile.remote_ok = form.remote_ok.data
        profile.relocation_assistance = form.relocation_assistance.data
        profile.privacy_level = form.privacy_level.data
        profile.profile_type = form.profile_type.data
        # M3: opt-in to show your name in Pro users' profile viewer lists.
        profile.profile_view_names = form.profile_view_names.data
        
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
    analytics = get_analytics(current_user.id)
    level, trust_score, _breakdown = trust_level(current_user)
    # M2: pending verification request (if any) and open SLA breaches
    # (pitches on the user's needs that are past their response
    # deadline) — shown on the own-profile page so the rule is visible.
    verification_request = VerificationRequest.query.filter_by(
        user_id=current_user.id, status='pending'
    ).first()
    open_breaches = [b for b in sla_breaches(current_user.id) if b['open']]
    # Phase 1 profile header: tappable stats, trust row, and a Posts
    # tab rendered with the shared post-card component.
    from blueprints.social.forms import ShareForm
    from blueprints.social.routes import (
        _author_levels, _author_profiles, _liked_and_shared_ids,
        _poll_votes, _saved_ids,
    )
    followers_count = Follow.query.filter_by(
        followed_id=current_user.id).count()
    following_count = Follow.query.filter_by(
        follower_id=current_user.id).count()
    posts = Post.query.filter_by(
        author_id=current_user.id, is_hidden=False
    ).order_by(desc(Post.created_at)).limit(20).all()
    liked_ids, shared_ids = _liked_and_shared_ids([p.id for p in posts])
    saved_ids = _saved_ids([p.id for p in posts])
    poll_votes = _poll_votes([p.id for p in posts])
    pledge = Pledge.query.filter_by(user_id=current_user.id).first()
    # Phase 3 trust visibility: vouches (endorsements + endorser
    # avatars), the verification tier badge, and portable reputation
    # metrics — all surfaced from existing data.
    from utils.reputation import reputation_metrics
    from utils.verification_tiers import verification_tier
    endorsements = endorsements_for(current_user.id)
    endorser_ids = {e.endorser_id for e in endorsements}
    endorser_profiles = {
        p.user_id: p
        for p in Profile.query.filter(
            Profile.user_id.in_(endorser_ids)
        ).all()
    } if endorser_ids else {}
    # Phase 3 hiring strip: the user's open referral needs (max 3).
    open_needs = Bounty.query.filter_by(
        poster_id=current_user.id, status='open'
    ).order_by(desc(Bounty.created_at)).limit(3).all()

    return render_template('profile/view.html',
                        profile=profile,
                        pitches_count=pitches_count,
                        matches_count=matches_count,
                        disputes_count=disputes_count,
                        reputation=reputation,
                        analytics=analytics,
                        trust_level=level,
                        trust_score=trust_score,
                        verification_request=verification_request,
                        open_breaches=open_breaches,
                        followers_count=followers_count,
                        following_count=following_count,
                        posts=posts,
                        liked_ids=liked_ids,
                        shared_ids=shared_ids,
                        saved_ids=saved_ids,
                        poll_votes=poll_votes,
                        author_levels=_author_levels(posts),
                        author_profiles=_author_profiles(posts),
                        share_form=ShareForm(),
                        pledge=pledge,
                        endorsements=endorsements,
                        endorser_profiles=endorser_profiles,
                        verification_tier=verification_tier(current_user),
                        reputation_metrics=reputation_metrics(
                            current_user.id),
                        open_needs=open_needs,
                        verified_linked_count=verified_linked_count(
                            current_user.id))


@profile_bp.route('/verify-request', methods=['POST'])
@login_required
def verify_request():
    """POST /profile/verify-request - Request admin verification (M2).
    One pending request at a time."""
    profile = Profile.query.filter_by(user_id=current_user.id).first()
    if profile and profile.verified:
        flash('Your profile is already verified.', 'info')
        return redirect(url_for('profile.view'))
    existing = VerificationRequest.query.filter_by(
        user_id=current_user.id, status='pending'
    ).first()
    if existing:
        flash('You already have a pending verification request.', 'info')
        return redirect(url_for('profile.view'))
    note = (request.form.get('note') or '').strip() or None
    req = VerificationRequest(user_id=current_user.id, note=note)
    db.session.add(req)
    db.session.commit()
    flash('Verification requested! An admin will review it.', 'success')
    return redirect(url_for('profile.view'))


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

    # M1: record the view (once per session; never for your own profile),
    # follow state, and trust info for the public card.
    viewer_id = current_user.id if current_user.is_authenticated else None
    if viewer_id and viewer_id != user_id:
        from flask import session
        seen = session.get('profile_views_seen', [])
        if user_id not in seen:
            db.session.add(ProfileView(profile_user_id=user_id, viewer_id=viewer_id))
            db.session.commit()
            seen.append(user_id)
            session['profile_views_seen'] = seen
    is_following = bool(viewer_id) and Follow.query.filter_by(
        follower_id=viewer_id, followed_id=user_id
    ).first() is not None
    followers_count = Follow.query.filter_by(followed_id=user_id).count()
    following_count = Follow.query.filter_by(follower_id=user_id).count()
    level, trust_score, _breakdown = trust_level(user)
    # M2: revealed reviews + rating summary for the public card.
    reviews = revealed_reviews_for_user(user_id)
    ratings_avg, ratings_count, _positive = ratings_summary(user_id)
    # M3: endorsements, visible linked accounts, "Verified elsewhere"
    # marker (2+ verified links), and the user's pledge if any.
    endorsements = endorsements_for(user_id)
    linked_accounts = visible_linked_accounts(user_id, viewer_id)
    verified_elsewhere = verified_linked_count(user_id) >= 2
    pledge = Pledge.query.filter_by(user_id=user_id, status='active').first()
    # Phase 3 trust visibility: endorser avatars for the vouch graph,
    # the verification tier badge, and portable reputation metrics.
    from utils.reputation import reputation_metrics
    from utils.verification_tiers import verification_tier
    endorser_ids = {e.endorser_id for e in endorsements}
    endorser_profiles = {
        p.user_id: p
        for p in Profile.query.filter(
            Profile.user_id.in_(endorser_ids)
        ).all()
    } if endorser_ids else {}
    # Phase 3 hiring strip: the user's open referral needs (max 3).
    open_needs = Bounty.query.filter_by(
        poster_id=user_id, status='open'
    ).order_by(desc(Bounty.created_at)).limit(3).all()
    # Phase 1: the Posts tab renders the shared post-card component.
    from blueprints.social.forms import ShareForm
    from blueprints.social.routes import (
        _author_levels, _author_profiles, _liked_and_shared_ids,
        _poll_votes, _saved_ids,
    )
    posts = Post.query.filter_by(
        author_id=user_id, is_hidden=False
    ).order_by(desc(Post.created_at)).limit(20).all()
    liked_ids, shared_ids = _liked_and_shared_ids([p.id for p in posts])
    saved_ids = _saved_ids([p.id for p in posts])
    poll_votes = _poll_votes([p.id for p in posts])

    return render_template('profile/public.html',
                        user=user, profile=profile,
                        pitches_count=pitches_count,
                        matches_count=matches_count,
                        disputes_count=disputes_count,
                        reputation=reputation,
                        is_following=is_following,
                        followers_count=followers_count,
                        following_count=following_count,
                        trust_level=level,
                        trust_score=trust_score,
                        reviews=reviews,
                        ratings_avg=ratings_avg,
                        ratings_count=ratings_count,
                        endorsements=endorsements,
                        endorser_profiles=endorser_profiles,
                        verification_tier=verification_tier(user),
                        reputation_metrics=reputation_metrics(user_id),
                        open_needs=open_needs,
                        linked_accounts=linked_accounts,
                        verified_elsewhere=verified_elsewhere,
                        pledge=pledge,
                        endorse_form=EndorseForm(),
                        posts=posts,
                        liked_ids=liked_ids,
                        shared_ids=shared_ids,
                        saved_ids=saved_ids,
                        poll_votes=poll_votes,
                        author_levels=_author_levels(posts),
                        author_profiles=_author_profiles(posts),
                        share_form=ShareForm())


# ---------------------------------------------------------------------------
# Linked accounts (M3, §2)
# ---------------------------------------------------------------------------

@profile_bp.route('/linked')
@login_required
def linked_accounts():
    """GET /profile/linked — manage linked social accounts. Expired
    verifications are flipped here (a page load, not the trust
    computation, does the write)."""
    refresh_linked_account_expiry()
    db.session.commit()
    accounts = LinkedAccount.query.filter_by(user_id=current_user.id).all()
    by_platform = {a.platform: a for a in accounts}
    return render_template(
        'profile/linked.html',
        accounts=accounts,
        by_platform=by_platform,
        platforms=PLATFORMS,
        oauth_platforms=OAUTH_PLATFORMS,
        oauth_available={
            p: oauth_available(p) for p in OAUTH_PLATFORMS
        },
        form=LinkedAccountForm(),
        verified_count=verified_linked_count(current_user.id),
    )


@profile_bp.route('/linked/<platform>/add', methods=['POST'])
@login_required
def add_linked_account(platform):
    """POST /profile/linked/<platform>/add — link an account for
    bio-link or code verification (OAuth has its own flow)."""
    if platform not in PLATFORMS:
        flash('Unknown platform.', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    form = LinkedAccountForm()
    if not form.validate_on_submit():
        flash('Check the link details and try again.', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    method = form.method.data
    if method not in ('bio_link', 'code'):
        flash('Choose bio-link or code verification (or use OAuth).', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    account = get_or_create_link(
        current_user.id, platform, method,
        url=(form.url.data or '').strip() or None,
        handle=(form.handle.data or '').strip() or None,
    )
    account.visibility = form.visibility.data or 'public'
    db.session.commit()
    if method == 'code':
        flash(
            f'Post this code on your {platform} profile, then click '
            f'Check: {account.verification_code}',
            'success',
        )
    else:
        flash(
            'Add your Paid2Match profile link to your bio on that '
            'platform, then click Check.',
            'success',
        )
    return redirect(url_for('profile.linked_accounts'))


@profile_bp.route('/linked/<platform>/oauth')
@login_required
def linked_oauth_start(platform):
    """GET /profile/linked/<platform>/oauth — start the OAuth flow
    (LinkedIn, GitHub)."""
    if platform not in OAUTH_PLATFORMS or not oauth_available(platform):
        flash('OAuth is not available for that platform.', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    state = oauth_state_token()
    session['oauth_state'] = state
    session['oauth_platform'] = platform
    redirect_uri = url_for('profile.linked_oauth_callback', platform=platform, _external=True)
    authorize_url = oauth_authorize_url(platform, redirect_uri, state)
    if not authorize_url:
        flash('OAuth is not configured for that platform.', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    return redirect(authorize_url)


@profile_bp.route('/linked/<platform>/oauth/callback')
@login_required
def linked_oauth_callback(platform):
    """GET /profile/linked/<platform>/oauth/callback — finish OAuth:
    verify the state token, exchange the code, fetch the profile,
    mark the link verified."""
    if platform not in OAUTH_PLATFORMS:
        flash('Unknown platform.', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    state = request.args.get('state')
    if not state or state != session.pop('oauth_state', None):
        flash('The sign-in did not verify — please try again.', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    session.pop('oauth_platform', None)
    code = request.args.get('code')
    if not code:
        flash('The sign-in was cancelled.', 'warning')
        return redirect(url_for('profile.linked_accounts'))
    redirect_uri = url_for('profile.linked_oauth_callback', platform=platform, _external=True)
    token, error = exchange_oauth_code(platform, code, redirect_uri)
    if error:
        flash(error, 'danger')
        return redirect(url_for('profile.linked_accounts'))
    handle, profile_url, error = fetch_oauth_profile(platform, token)
    if error:
        flash(error, 'danger')
        return redirect(url_for('profile.linked_accounts'))
    account = get_or_create_link(
        current_user.id, platform, 'oauth',
        url=profile_url, handle=handle,
    )
    mark_verified(account)
    db.session.commit()
    flash(f'Your {platform} account is verified!', 'success')
    return redirect(url_for('profile.linked_accounts'))


@profile_bp.route('/linked/<account_id>/check', methods=['POST'])
@login_required
def check_linked_account(account_id):
    """POST /profile/linked/<id>/check — re-run the bio-link or code
    check for a pending/expired link."""
    account = get_or_404(LinkedAccount, account_id)
    if account.user_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    if account.status == 'verified' and account.is_currently_verified:
        flash('This link is already verified.', 'info')
        return redirect(url_for('profile.linked_accounts'))
    if account.verification_method == 'bio_link':
        profile_url = url_for(
            'profile.public_profile', user_id=current_user.id, _external=True
        )
        ok, error = verify_bio_link(account, profile_url)
    elif account.verification_method == 'code':
        ok, error = verify_code(account)
    else:
        flash('Use the sign-in flow to verify this link.', 'info')
        return redirect(url_for('profile.linked_accounts'))
    if ok:
        mark_verified(account)
        db.session.commit()
        flash(f'Your {account.platform} account is verified!', 'success')
    else:
        db.session.commit()
        flash(error, 'warning')
    return redirect(url_for('profile.linked_accounts'))


@profile_bp.route('/linked/<account_id>/manual-review', methods=['POST'])
@login_required
def request_manual_review(account_id):
    """POST /profile/linked/<id>/manual-review — ask the founder to
    check the link by hand (for platforms that block automated
    fetches)."""
    account = get_or_404(LinkedAccount, account_id)
    if account.user_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    account.manual_review = True
    account.status = 'pending'
    db.session.commit()
    flash('Manual review requested — the founder will check it.', 'success')
    return redirect(url_for('profile.linked_accounts'))


@profile_bp.route('/linked/<account_id>/unlink', methods=['POST'])
@login_required
def unlink_account(account_id):
    """POST /profile/linked/<id>/unlink — remove a link entirely
    (Law 25: linking is revocable; unlinking deletes the record)."""
    account = get_or_404(LinkedAccount, account_id)
    if account.user_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    db.session.delete(account)
    db.session.commit()
    flash('Link removed.', 'info')
    return redirect(url_for('profile.linked_accounts'))


@profile_bp.route('/linked/<account_id>/visibility', methods=['POST'])
@login_required
def set_link_visibility(account_id):
    """POST /profile/linked/<id>/visibility — public / followers /
    hidden (§2.4)."""
    account = get_or_404(LinkedAccount, account_id)
    if account.user_id != current_user.id:
        flash('Access denied', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    visibility = request.form.get('visibility')
    if visibility not in ('public', 'followers', 'hidden'):
        flash('Unknown visibility.', 'danger')
        return redirect(url_for('profile.linked_accounts'))
    account.visibility = visibility
    db.session.commit()
    flash('Link visibility updated.', 'success')
    return redirect(url_for('profile.linked_accounts'))


# ---------------------------------------------------------------------------
# Endorsements (M3, §3.3)
# ---------------------------------------------------------------------------

@profile_bp.route('/users/<user_id>/endorse', methods=['POST'])
@login_required
def endorse_user(user_id):
    """POST /profile/users/<id>/endorse — endorse someone for a skill."""
    endorsee = get_or_404(User, user_id)
    form = EndorseForm()
    if not form.validate_on_submit():
        flash('Add a skill to endorse for.', 'danger')
        return redirect(url_for('profile.public_profile', user_id=user_id))
    _endorsement, error = give_endorsement(
        current_user, endorsee,
        form.skill.data.strip(),
        form.comment.data,
    )
    if error:
        flash(error, 'warning')
    else:
        db.session.commit()
        flash(f'You endorsed {endorsee.full_name} for {form.skill.data.strip()}.', 'success')
    return redirect(url_for('profile.public_profile', user_id=user_id))
