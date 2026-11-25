"""M1 tests: posts, feed, follows, likes/comments/shares, people search."""
from datetime import datetime, timedelta, timezone

import pytest

from extensions import db
from models.bounty import Bounty
from models.follow import Follow
from models.post import Comment, Like, Post, Share
from utils.feed import apply_need_cap, discover_score, get_discover_feed, get_following_feed


def _post(author_id, body, post_type='update', hours_ago=0, bounty_id=None):
    post = Post(
        author_id=author_id, post_type=post_type, body=body, bounty_id=bounty_id,
    )
    db.session.add(post)
    db.session.commit()
    if hours_ago:
        post.created_at = datetime.now(timezone.utc) - timedelta(hours=hours_ago)
        db.session.commit()
    return post


class TestPostCRUD:
    def test_create_post(self, client, logged_in_user, sample_user):
        resp = client.post('/social/posts/new', data={
            'post_type': 'update',
            'body': 'A brand new post from the test suite.',
        }, follow_redirects=True)
        assert resp.status_code == 200
        post = Post.query.filter_by(author_id=sample_user.id).first()
        assert post is not None
        assert post.body == 'A brand new post from the test suite.'
        assert post.post_type == 'update'

    def test_create_post_requires_login(self, client):
        resp = client.post('/social/posts/new', data={
            'post_type': 'update', 'body': 'Nope.',
        })
        assert resp.status_code in (302, 401)

    def test_create_post_empty_body_rejected(self, client, logged_in_user):
        resp = client.post('/social/posts/new', data={
            'post_type': 'update', 'body': '',
        })
        # Form validation fails -> re-rendered form, no post created.
        assert Post.query.count() == 0

    def test_edit_post(self, client, logged_in_user, sample_post):
        post_id = sample_post.id
        resp = client.post(f'/social/posts/{post_id}/edit', data={
            'post_type': 'update',
            'body': 'Edited body, still long enough.',
        }, follow_redirects=True)
        assert resp.status_code == 200
        assert db.session.get(Post, post_id).body == 'Edited body, still long enough.'

    def test_edit_post_forbidden_for_others(self, client, login_as, second_user, sample_post):
        post_id = sample_post.id
        login_as(client, 'second@example.com', 'SecondPass123!')
        resp = client.post(f'/social/posts/{post_id}/edit', data={
            'post_type': 'update', 'body': 'Hacked body, long enough.',
        })
        assert resp.status_code in (302, 403)
        assert db.session.get(Post, post_id).body != 'Hacked body, long enough.'

    def test_delete_post(self, client, logged_in_user, sample_post):
        resp = client.post(f'/social/posts/{sample_post.id}/delete', follow_redirects=True)
        assert resp.status_code == 200
        assert db.session.get(Post, sample_post.id) is None

    def test_post_detail_renders(self, client, sample_post):
        resp = client.get(f'/social/posts/{sample_post.id}')
        assert resp.status_code == 200
        assert b'Hello network' in resp.data


class TestLikesCommentsShares:
    def test_like_toggle(self, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        post_id = sample_post.id
        client.post(f'/social/posts/{post_id}/like')
        assert Like.query.filter_by(
            user_id=second_user.id, post_id=post_id).first() is not None
        assert db.session.get(Post, post_id).like_count == 1
        # Second click unlikes.
        client.post(f'/social/posts/{post_id}/like')
        assert Like.query.filter_by(
            user_id=second_user.id, post_id=post_id).first() is None
        assert db.session.get(Post, post_id).like_count == 0

    def test_like_awards_author_karma(self, client, login_as, second_user, sample_post, sample_user):
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(f'/social/posts/{sample_post.id}/like')
        from models.profile import Profile
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        assert profile.karma == 10

    def test_comment_and_one_level_reply(self, client, login_as, second_user, third_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(f'/social/posts/{sample_post.id}/comment', data={
            'body': 'Great post, thanks for sharing this with us all.',
        })
        comment = Comment.query.filter_by(post_id=sample_post.id).first()
        assert comment is not None and comment.parent_id is None
        # Reply to the comment (one level).
        login_as(client, 'third@example.com', 'ThirdPass123!')
        client.post(f'/social/posts/{sample_post.id}/comment', data={
            'body': 'Agreed — this is a very helpful reply indeed.',
            'parent_id': comment.id,
        })
        reply = Comment.query.filter_by(parent_id=comment.id).first()
        assert reply is not None
        # Replying to a reply is rejected (one level only).
        resp = client.post(f'/social/posts/{sample_post.id}/comment', data={
            'body': 'A third level that should not be allowed here.',
            'parent_id': reply.id,
        }, follow_redirects=True)
        assert Comment.query.filter_by(parent_id=reply.id).first() is None

    def test_share_creates_unique_share(self, client, login_as, second_user, sample_post):
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(f'/social/posts/{sample_post.id}/share', data={'comment': 'Worth sharing!'})
        assert Share.query.filter_by(
            user_id=second_user.id, post_id=sample_post.id).count() == 1
        # Sharing again does not duplicate.
        client.post(f'/social/posts/{sample_post.id}/share', data={'comment': 'Again!'})
        assert Share.query.filter_by(
            user_id=second_user.id, post_id=sample_post.id).count() == 1
        assert db.session.get(Post, sample_post.id).share_count == 1


class TestFollowGraph:
    def test_follow_and_unfollow(self, client, logged_in_user, second_user, sample_user):
        resp = client.post(f'/social/follow/{second_user.id}', follow_redirects=True)
        assert resp.status_code == 200
        assert Follow.query.filter_by(
            follower_id=sample_user.id, followed_id=second_user.id).first() is not None
        client.post(f'/social/unfollow/{second_user.id}')
        assert Follow.query.filter_by(
            follower_id=sample_user.id, followed_id=second_user.id).first() is None

    def test_cannot_follow_self(self, client, logged_in_user, sample_user):
        client.post(f'/social/follow/{sample_user.id}')
        assert Follow.query.filter_by(
            follower_id=sample_user.id, followed_id=sample_user.id).first() is None

    def test_follow_is_idempotent(self, client, logged_in_user, second_user, sample_user):
        client.post(f'/social/follow/{second_user.id}')
        client.post(f'/social/follow/{second_user.id}')
        assert Follow.query.filter_by(
            follower_id=sample_user.id, followed_id=second_user.id).count() == 1

    def test_followers_and_following_lists(self, client, logged_in_user, second_user, sample_user):
        client.post(f'/social/follow/{second_user.id}')
        resp = client.get(f'/social/users/{second_user.id}/followers')
        assert resp.status_code == 200
        assert b'Test User' in resp.data
        resp = client.get(f'/social/users/{sample_user.id}/following')
        assert resp.status_code == 200
        assert b'Second User' in resp.data


class TestFeeds:
    def test_following_feed_is_chronological(self, app, sample_user, second_user, third_user):
        db.session.add(Follow(follower_id=sample_user.id, followed_id=second_user.id))
        old = _post(second_user.id, 'Old post from second user.', hours_ago=5)
        new = _post(second_user.id, 'New post from second user.', hours_ago=1)
        _post(third_user.id, 'Not followed — should not appear.')
        feed = get_following_feed(sample_user.id)
        ids = [item['post'].id for item in feed['items']]
        assert ids == [new.id, old.id]

    def test_following_feed_includes_own_posts_and_shares(self, app, sample_user, second_user, sample_post):
        db.session.add(Follow(follower_id=sample_user.id, followed_id=second_user.id))
        other = _post(second_user.id, 'Second user post for the feed.')
        db.session.add(Share(user_id=second_user.id, post_id=sample_post.id))
        db.session.commit()
        feed = get_following_feed(sample_user.id)
        kinds = {item['kind'] for item in feed['items']}
        assert 'post' in kinds and 'share' in kinds
        post_ids = {item['post'].id for item in feed['items']}
        assert sample_post.id in post_ids and other.id in post_ids

    def test_discover_excludes_own_posts(self, app, sample_user, second_user, sample_post):
        _post(second_user.id, 'Someone else post for discover.')
        feed = get_discover_feed(sample_user.id)
        ids = [item['post'].id for item in feed['items']]
        assert sample_post.id not in ids
        assert len(ids) == 1

    def test_discover_engagement_beats_equal_age(self, app, sample_user, second_user, third_user):
        # Same age: the post with likes scores higher.
        loved = _post(second_user.id, 'Loved post with likes here.', hours_ago=2)
        plain = _post(third_user.id, 'Plain post with no engagement.', hours_ago=2)
        db.session.add(Like(user_id=sample_user.id, post_id=loved.id))
        db.session.add(Like(user_id=third_user.id, post_id=loved.id))
        db.session.commit()
        assert discover_score(loved) > discover_score(plain)

    def test_discover_recency_beats_age(self, app, sample_user, second_user, third_user):
        # Same engagement (none): the newer post scores higher.
        old = _post(second_user.id, 'Old post with no engagement.', hours_ago=40)
        fresh = _post(third_user.id, 'Fresh post with no engagement.', hours_ago=1)
        assert discover_score(fresh) > discover_score(old)

    def test_need_cap_interleaves_one_in_three(self, app, sample_user, second_user):
        needs = [
            _post(second_user.id, f'Need post number {i}.', post_type='need_paid')
            for i in range(4)
        ]
        others = [
            _post(second_user.id, f'Regular post number {i}.', post_type='update')
            for i in range(4)
        ]
        ranked = needs + others  # needs ranked first (worst case)
        capped = apply_need_cap(ranked)
        # First 3 positions hold at most 1 need; first 6 at most 2.
        assert sum(1 for p in capped[:3] if p.is_need) <= 1
        assert sum(1 for p in capped[:6] if p.is_need) <= 2
        # Nothing is dropped — all 8 still appear.
        assert len(capped) == 8
        assert {p.id for p in capped} == {p.id for p in ranked}

    def test_feed_page_renders_both_tabs(self, client, logged_in_user, sample_post):
        resp = client.get('/social/feed?tab=following')
        assert resp.status_code == 200
        assert b'Hello network' in resp.data
        resp = client.get('/social/feed?tab=discover')
        assert resp.status_code == 200
        # Phase 2 renamed the Discover tab to Top (the tab=discover
        # URL is kept for existing links).
        assert b'How Top ranks posts' in resp.data


class TestPeopleSearch:
    def test_search_by_name(self, client, logged_in_user, second_user):
        resp = client.get('/social/people?q=Second')
        assert resp.status_code == 200
        assert b'Second User' in resp.data

    def test_search_by_skill(self, client, logged_in_user, second_user):
        resp = client.get('/social/people?skill=Sales')
        assert resp.status_code == 200
        assert b'Second User' in resp.data

    def test_search_by_location(self, client, logged_in_user, third_user):
        resp = client.get('/social/people?location=Laval')
        assert resp.status_code == 200
        assert b'Third User' in resp.data

    def test_suggested_connections_same_city(self, app, sample_user, second_user, third_user):
        from models.profile import Profile
        from utils.feed import suggested_connections
        my_profile = Profile.query.filter_by(user_id=sample_user.id).first()
        my_profile.location = 'Montreal, QC'
        db.session.commit()
        suggested = suggested_connections(sample_user.id)
        ids = [u.id for u in suggested]
        # Phase 3: suggestions are score-based (shared spaces, city,
        # karma, referrals, trust) — the same-city user ranks first,
        # but a different-city user is still suggested (never an
        # empty rail), and you are never suggested to yourself.
        assert second_user.id in ids
        assert ids.index(second_user.id) < ids.index(third_user.id)
        assert sample_user.id not in ids


class TestNeedPosts:
    def test_posting_a_need_creates_a_feed_post(self, client, logged_in_user, sample_user):
        resp = client.post('/needs/post', data={
            'title': 'Need a senior recruiter for a startup',
            'description': 'We are looking for a senior recruiter with startup experience and a strong network.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
            'reward_amount': '0',
            'location': 'Remote',
            'is_free': '1',
        }, follow_redirects=True)
        assert resp.status_code == 200
        bounty = Bounty.query.filter_by(title='Need a senior recruiter for a startup').first()
        assert bounty is not None
        post = Post.query.filter_by(bounty_id=bounty.id).first()
        assert post is not None
        assert post.post_type == 'need_free'
        assert post.body == bounty.description

    def test_posting_a_paid_need_creates_need_paid_post(self, client, logged_in_user, sample_user):
        resp = client.post('/needs/post', data={
            'title': 'Need a senior engineer, paid bounty',
            'description': 'We are looking for a senior engineer with a proven track record in fintech.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
            'reward_amount': '2000',
            'location': 'Remote',
        }, follow_redirects=True)
        assert resp.status_code == 200
        bounty = Bounty.query.filter_by(title='Need a senior engineer, paid bounty').first()
        post = Post.query.filter_by(bounty_id=bounty.id).first()
        assert post is not None
        assert post.post_type == 'need_paid'

    def test_convert_post_to_free_need(self, client, logged_in_user, sample_post, sample_user):
        resp = client.post(f'/social/posts/{sample_post.id}/convert', data={
            'need_kind': 'free',
            'title': 'Converted need from a post',
            'location': 'Remote',
        }, follow_redirects=True)
        assert resp.status_code == 200
        post = db.session.get(Post, sample_post.id)
        assert post.post_type == 'need_free'
        assert post.bounty_id is not None
        bounty = db.session.get(Bounty, post.bounty_id)
        assert bounty.title == 'Converted need from a post'
        assert bounty.is_free is True

    def test_convert_post_to_paid_need_redirects_to_payment(self, client, logged_in_user, sample_post):
        resp = client.post(f'/social/posts/{sample_post.id}/convert', data={
            'need_kind': 'paid',
            'title': 'Converted paid need from a post',
            'reward_amount': '1500',
            'location': 'Remote',
        })
        assert resp.status_code == 302
        assert 'secure-payment' in resp.location
        assert db.session.get(Post, sample_post.id).post_type == 'need_paid'

    def test_editing_a_need_syncs_the_post(self, client, logged_in_user, sample_bounty):
        # Create the linked post first (fixture bounties predate M1 hooks).
        from utils.feed import create_need_post
        post = create_need_post(sample_bounty)
        db.session.commit()
        resp = client.post(f'/needs/{sample_bounty.id}/edit', data={
            'title': sample_bounty.title,
            'description': 'Updated description for the synced post test — long enough to pass validation.',
            'bounty_type': 'recruitment',
            'bounty_direction': 'seeking_opportunity',
            'reward_amount': '0',
            'location': 'Remote',
            'success_criteria': '',
        }, follow_redirects=True)
        assert resp.status_code == 200
        post_id = post.id
        assert db.session.get(Post, post_id).body == 'Updated description for the synced post test — long enough to pass validation.'


def _paid_bounty(poster_id, title, secured=False):
    """A paid bounty: unsecured by default (payment not yet escrowed)."""
    bounty = Bounty(
        poster_id=poster_id,
        title=title,
        description='A paid referral need with a real reward attached.',
        bounty_type='recruitment',
        bounty_direction='seeking_opportunity',
        reward_amount=2000,
        status='open',
        payment_status='secured' if secured else 'unsecured',
        is_free=False,
    )
    db.session.add(bounty)
    db.session.commit()
    return bounty


def _need_post(bounty):
    from utils.feed import create_need_post
    post = create_need_post(bounty)
    db.session.commit()
    return post


class TestAuditFixes:
    """Regression tests for the 2026-11-24 social-core audit."""

    def test_like_unlike_like_awards_karma_once(
            self, client, login_as, second_user, third_user, sample_post,
            sample_user):
        # Like -> unlike -> like must not farm +10 karma per cycle.
        from models.profile import Profile
        from models.referral import KarmaEvent
        login_as(client, 'second@example.com', 'SecondPass123!')
        client.post(f'/social/posts/{sample_post.id}/like')
        client.post(f'/social/posts/{sample_post.id}/like')  # unlike
        client.post(f'/social/posts/{sample_post.id}/like')  # like again
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        assert profile.karma == 10
        assert KarmaEvent.query.filter_by(
            user_id=sample_user.id, reason='like_received').count() == 1
        # A different liker still earns the author a fresh +10.
        login_as(client, 'third@example.com', 'ThirdPass123!')
        client.post(f'/social/posts/{sample_post.id}/like')
        profile = Profile.query.filter_by(user_id=sample_user.id).first()
        assert profile.karma == 20

    def test_delete_post_cleans_up_children(
            self, client, logged_in_user, app, sample_user, second_user,
            sample_post):
        from datetime import datetime, timezone
        from models.bump import Boost, Bump, RecurringBump
        from models.poll import Poll, PollOption, PollVote, create_poll
        from models.post import Bookmark
        from models.post_view import PostView
        post_id = sample_post.id
        poll = create_poll(sample_post, ['Option A', 'Option B'])
        db.session.flush()
        option = PollOption.query.filter_by(poll_id=poll.id).first()
        db.session.add(PollVote(
            poll_id=poll.id, option_id=option.id, user_id=second_user.id))
        db.session.add(Bookmark(
            user_id=second_user.id, post_id=post_id))
        db.session.add(PostView(
            post_id=post_id, viewer_id=second_user.id))
        now = datetime.now(timezone.utc)
        db.session.add(Bump(
            post_id=post_id, user_id=sample_user.id, source='free',
            expires_at=now + timedelta(days=1)))
        db.session.add(Boost(
            post_id=post_id, user_id=sample_user.id, days=1,
            price_cents=500, stripe_session_id='sess_audit_1',
            starts_at=now, ends_at=now + timedelta(days=1)))
        db.session.add(RecurringBump(
            post_id=post_id, user_id=sample_user.id, status='canceled',
            next_bump_at=now + timedelta(days=3)))
        db.session.commit()
        resp = client.post(
            f'/social/posts/{post_id}/delete', follow_redirects=True)
        assert resp.status_code == 200
        assert db.session.get(Post, post_id) is None
        assert Poll.query.filter_by(post_id=post_id).first() is None
        assert PollVote.query.filter_by(poll_id=poll.id).count() == 0
        assert Bookmark.query.filter_by(post_id=post_id).count() == 0
        assert PostView.query.filter_by(post_id=post_id).count() == 0
        assert Bump.query.filter_by(post_id=post_id).count() == 0
        assert Boost.query.filter_by(post_id=post_id).count() == 0
        assert RecurringBump.query.filter_by(post_id=post_id).count() == 0

    def test_delete_post_blocked_by_active_recurring_bump(
            self, client, logged_in_user, app, sample_user, sample_post):
        from datetime import datetime, timezone
        from models.bump import RecurringBump
        db.session.add(RecurringBump(
            post_id=sample_post.id, user_id=sample_user.id,
            status='active',
            next_bump_at=datetime.now(timezone.utc) + timedelta(days=3)))
        db.session.commit()
        resp = client.post(
            f'/social/posts/{sample_post.id}/delete', follow_redirects=True)
        assert resp.status_code == 200
        # The post survives — deleting it would orphan a live Stripe
        # subscription with no row left to cancel.
        assert db.session.get(Post, sample_post.id) is not None
        assert b'recurring bump' in resp.data

    def test_unsecured_paid_need_hidden_everywhere(
            self, app, client, login_as, sample_user, second_user):
        from utils.feed import (
            get_discover_feed, get_following_feed, get_latest_feed,
            get_trending_feed,
        )
        bounty = _paid_bounty(second_user.id, 'Unsecured paid need')
        post = _need_post(bounty)
        db.session.add(Follow(
            follower_id=sample_user.id, followed_id=second_user.id))
        db.session.commit()
        for getter in (get_following_feed, get_latest_feed,
                       get_discover_feed, get_trending_feed):
            ids = [i['post'].id for i in getter(sample_user.id)['items']]
            assert post.id not in ids
        # Search does not leak it either (the post body matches the
        # query, but the post is not public yet).
        login_as(client, 'testuser@example.com', 'TestPass123!')
        resp = client.get('/social/search?q=real+reward+attached')
        assert resp.status_code == 200
        assert b'A paid referral need' not in resp.data
        # Post detail redirects non-authors; liking 404s.
        resp = client.get(f'/social/posts/{post.id}')
        assert resp.status_code == 302
        resp = client.post(f'/social/posts/{post.id}/like')
        assert resp.status_code == 404
        # The author can still view it (they need the page to pay).
        login_as(client, 'second@example.com', 'SecondPass123!')
        resp = client.get(f'/social/posts/{post.id}')
        assert resp.status_code == 200
        # Once payment is secured, the need goes public everywhere.
        bounty.payment_status = 'secured'
        db.session.commit()
        ids = [i['post'].id
               for i in get_latest_feed(sample_user.id)['items']]
        assert post.id in ids

    def test_latest_feed_hides_shares_by_blocked_user(
            self, app, sample_user, second_user, third_user):
        from models.moderation import Block
        from utils.feed import get_latest_feed
        post = _post(third_user.id, 'A post worth sharing around.')
        db.session.add(Share(user_id=second_user.id, post_id=post.id))
        db.session.add(Block(
            blocker_id=sample_user.id, blocked_id=second_user.id,
            kind='block'))
        db.session.commit()
        items = get_latest_feed(sample_user.id)['items']
        assert not any(
            i['kind'] == 'share' and i['share'].user_id == second_user.id
            for i in items
        )
        # The underlying post itself still shows — only the blocked
        # user's share is hidden.
        assert any(
            i['kind'] == 'post' and i['post'].id == post.id
            for i in items
        )

    def test_following_feed_hides_shares_by_muted_user(
            self, app, sample_user, second_user, third_user):
        from models.moderation import Block
        from utils.feed import get_following_feed
        db.session.add(Follow(
            follower_id=sample_user.id, followed_id=second_user.id))
        post = _post(third_user.id, 'Another post worth sharing.')
        db.session.add(Share(user_id=second_user.id, post_id=post.id))
        db.session.add(Block(
            blocker_id=sample_user.id, blocked_id=second_user.id,
            kind='mute'))
        db.session.commit()
        items = get_following_feed(sample_user.id)['items']
        assert not any(
            i['kind'] == 'share' and i['share'].user_id == second_user.id
            for i in items
        )

    def test_negative_page_clamped_to_first(
            self, app, sample_user, second_user):
        from utils.feed import get_following_feed
        db.session.add(Follow(
            follower_id=sample_user.id, followed_id=second_user.id))
        _post(second_user.id, 'Only post in the feed here.')
        first = get_following_feed(sample_user.id, page=1)
        negative = get_following_feed(sample_user.id, page=-1)
        zero = get_following_feed(sample_user.id, page=0)
        assert negative['page'] == 1
        assert zero['page'] == 1
        assert [i['post'].id for i in negative['items']] == [
            i['post'].id for i in first['items']]

    def test_edit_need_post_redirects_to_bounty(
            self, client, logged_in_user, sample_user):
        bounty = _paid_bounty(
            sample_user.id, 'A need to edit', secured=True)
        post = _need_post(bounty)
        resp = client.get(f'/social/posts/{post.id}/edit')
        assert resp.status_code == 302
        assert f'/needs/{bounty.id}/edit' in resp.location
