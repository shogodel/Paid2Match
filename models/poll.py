"""Polls (Phase 2, Blind-style): a post can carry a poll with 2–6
options. One vote per user per poll. Results are blind — hidden until
you vote (or you are the post's author) — and shown as bars with
percentages once revealed.
"""
from extensions import db
from models import generate_uuid, utc_now

# A poll needs at least 2 options to be a question, and 6 keeps the
# card scannable.
POLL_MIN_OPTIONS = 2
POLL_MAX_OPTIONS = 6
POLL_OPTION_MAX_LENGTH = 100


class Poll(db.Model):
    """A poll attached to one post (one poll per post)."""
    __tablename__ = 'polls'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    post_id = db.Column(
        db.String(36), db.ForeignKey('posts.id'),
        nullable=False, unique=True, index=True,
    )
    created_at = db.Column(db.DateTime, default=utc_now)

    post = db.relationship(
        'Post',
        backref=db.backref('poll', uselist=False, cascade='all, delete-orphan'),
    )
    options = db.relationship(
        'PollOption', backref='poll', lazy='dynamic',
        cascade='all, delete-orphan',
        order_by='PollOption.position',
    )
    votes = db.relationship(
        'PollVote', backref='poll', lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        return f'<Poll {self.id} on {self.post_id}>'

    @property
    def total_votes(self):
        return self.votes.count()

    def vote_for(self, user_id):
        """The option this user voted for, or None."""
        if not user_id:
            return None
        vote = PollVote.query.filter_by(
            poll_id=self.id, user_id=user_id,
        ).first()
        return vote.option_id if vote else None

    def results(self):
        """Options with vote counts and percentages, for the chart."""
        total = self.total_votes
        return [
            {
                'id': option.id,
                'text': option.text,
                'votes': option.vote_count,
                'percent': round(option.vote_count * 100 / total) if total else 0,
            }
            for option in self.options.all()
        ]


class PollOption(db.Model):
    """One answer option of a poll."""
    __tablename__ = 'poll_options'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    poll_id = db.Column(db.String(36), db.ForeignKey('polls.id'), nullable=False, index=True)
    text = db.Column(db.String(POLL_OPTION_MAX_LENGTH), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)

    votes = db.relationship(
        'PollVote', backref='option', lazy='dynamic',
        cascade='all, delete-orphan',
    )

    def __repr__(self):
        return f'<PollOption {self.text!r}>'

    @property
    def vote_count(self):
        return self.votes.count()


class PollVote(db.Model):
    """One user's vote in one poll — the unique constraint is what
    makes it one vote per user."""
    __tablename__ = 'poll_votes'
    __table_args__ = (
        db.UniqueConstraint(
            'user_id', 'poll_id', name='uq_poll_vote_user_poll'
        ),
    )

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    poll_id = db.Column(db.String(36), db.ForeignKey('polls.id'), nullable=False, index=True)
    option_id = db.Column(db.String(36), db.ForeignKey('poll_options.id'), nullable=False, index=True)
    user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=utc_now)

    def __repr__(self):
        return f'<PollVote {self.user_id} -> {self.option_id}>'


def parse_poll_options(raw):
    """Parse the composer's poll textarea (one option per line) into a
    list of option strings, or return (None, error) when invalid."""
    if raw is None:
        return [], None
    options = [
        line.strip()
        for line in raw.splitlines()
        if line.strip()
    ]
    if not options:
        return [], None
    if len(options) < POLL_MIN_OPTIONS:
        return None, 'A poll needs at least 2 options.'
    if len(options) > POLL_MAX_OPTIONS:
        return None, 'A poll can have at most 6 options.'
    for option in options:
        if len(option) > POLL_OPTION_MAX_LENGTH:
            return None, 'Poll options must be 100 characters or less.'
    if len(set(options)) != len(options):
        return None, 'Poll options must be different from each other.'
    return options, None


def create_poll(post, options):
    """Attach a poll with these options to a post. Does not commit —
    the caller commits."""
    poll = Poll(post_id=post.id)
    db.session.add(poll)
    db.session.flush()
    for position, text in enumerate(options):
        db.session.add(PollOption(
            poll_id=poll.id, text=text, position=position,
        ))
    return poll
