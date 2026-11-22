"""Post views + people-search appearances (M3, §1.4 Pro analytics).

PostView: one recorded view of a post (the existing once-per-session
guard in the post-detail route still decides when a view counts — a
row is written only when the counter increments). Unique viewers per
post ("members reached") come from distinct viewer_ids; anonymous
views (viewer_id NULL) count as reached but are labeled an estimate.

SearchAppearance: one appearance in a people-search result list — the
Pro "search appearances" metric ("how often you appeared, and for
what"). Recorded only when a search filter is actually used.
"""
from extensions import db
from models import generate_uuid, utc_now


class PostView(db.Model):
    """One counted view of a post."""
    __tablename__ = 'post_views'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    post_id = db.Column(db.String(36), db.ForeignKey('posts.id'), nullable=False, index=True)
    viewer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utc_now, index=True)

    def __repr__(self):
        return f'<PostView {self.viewer_id} -> {self.post_id}>'


class SearchAppearance(db.Model):
    """One appearance of a user in someone's people-search results."""
    __tablename__ = 'search_appearances'

    id = db.Column(db.String(36), primary_key=True, default=generate_uuid)
    searched_user_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=False, index=True)
    # The search as the searcher typed it ("Python, Montreal" etc.).
    # NOTE: the search text is called search_query, not query — a
    # column named 'query' would shadow Flask-SQLAlchemy's
    # Model.query accessor and break SearchAppearance.query.filter.
    search_query = db.Column(db.String(300), nullable=True)
    viewer_id = db.Column(db.String(36), db.ForeignKey('users.id'), nullable=True, index=True)
    created_at = db.Column(db.DateTime, default=utc_now, index=True)

    def __repr__(self):
        return f'<SearchAppearance {self.searched_user_id} for {self.search_query!r}>'
