// Paid2Match — main JavaScript (Phase 1).
//
// Theme (data-bs-theme + localStorage p2m-theme), AJAX feed
// interactions with optimistic updates (like / comment / share /
// follow / mute / delete), the in-feed composer, the "New posts"
// pill, and toasts. No full page reloads for feed interactions.
// Alerts are dismissible by the user — they never auto-vanish.

document.addEventListener('DOMContentLoaded', function () {
    initTheme();
    initServiceWorker();
    initClamps(document);
    initComposer();
    initNewPostsPill();
    initDelegatedActions();
    initNotificationsPanel();
    initNotificationPoll();
    initTabIndicator();
});

// ---------------------------------------------------------------- PWA

function initServiceWorker() {
    // Phase 2: installable app. The worker precaches the shell and
    // the branding (never social data) and serves /offline when a
    // navigation fails. No custom install prompt — the browser shows
    // its default.
    if ('serviceWorker' in navigator) {
        navigator.serviceWorker.register('/sw.js').catch(function () {
            /* offline support is best-effort — stay silent */
        });
    }
}

// ---------------------------------------------------------------- theme

const THEME_KEY = 'p2m-theme';

function storedTheme() {
    try {
        return localStorage.getItem(THEME_KEY) || 'system';
    } catch (e) {
        return 'system';
    }
}

function effectiveTheme(pref) {
    if (pref === 'light' || pref === 'dark') return pref;
    return window.matchMedia('(prefers-color-scheme: dark)').matches
        ? 'dark'
        : 'light';
}

function applyTheme(pref) {
    document.documentElement.setAttribute(
        'data-bs-theme', effectiveTheme(pref)
    );
    document.querySelectorAll('.theme-check').forEach(function (el) {
        el.classList.toggle(
            'is-current', el.getAttribute('data-theme-check') === pref
        );
    });
}

function setTheme(pref) {
    try {
        localStorage.setItem(THEME_KEY, pref);
    } catch (e) { /* private mode — theme still applies this session */ }
    applyTheme(pref);
}

function initTheme() {
    // The <head> script already set the initial theme to prevent flash;
    // this syncs the menu checkmarks and wires the controls.
    applyTheme(storedTheme());

    document.querySelectorAll('.theme-toggle-btn').forEach(function (btn) {
        btn.addEventListener('click', function () {
            var current = document.documentElement.getAttribute('data-bs-theme');
            setTheme(current === 'dark' ? 'light' : 'dark');
        });
    });

    document.querySelectorAll('.theme-choice').forEach(function (btn) {
        btn.addEventListener('click', function () {
            setTheme(btn.getAttribute('data-theme'));
        });
    });

    // Follow the OS live while the preference is "system".
    window.matchMedia('(prefers-color-scheme: dark)')
        .addEventListener('change', function () {
            if (storedTheme() === 'system') applyTheme('system');
        });
}

// ---------------------------------------------------------------- fetch

function csrfToken() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    return meta ? meta.getAttribute('content') : '';
}

async function apiRequest(url, options) {
    options = options || {};
    var headers = {
        'X-Requested-With': 'XMLHttpRequest',
        'Accept': 'application/json',
    };
    if (options.method && options.method !== 'GET') {
        headers['X-CSRFToken'] = csrfToken();
    }
    if (options.json) {
        headers['Content-Type'] = 'application/json';
    }
    var response = await fetch(url, {
        method: options.method || 'GET',
        credentials: 'same-origin',
        headers: headers,
        body: options.body || undefined,
    });
    if (response.status === 401) {
        // Session expired (or a guest hit an interaction): send them to
        // login and back here afterwards.
        window.location.href = '/auth/login?next='
            + encodeURIComponent(window.location.pathname
                + window.location.search);
        throw new Error('auth');
    }
    var data = await response.json().catch(function () { return {}; });
    if (!response.ok) {
        throw new Error(data.error || 'Request failed');
    }
    return data;
}

// ---------------------------------------------------------------- toasts

function showToast(message, type, linkUrl, linkLabel) {
    type = type || 'success';
    var container = document.getElementById('p2m-toasts');
    if (!container) {
        container = document.createElement('div');
        container.id = 'p2m-toasts';
        container.style.cssText = 'position:fixed;right:1rem;bottom:1rem;'
            + 'z-index:2000;display:flex;flex-direction:column;gap:0.5rem;'
            + 'max-width:min(360px,calc(100vw - 2rem));';
        document.body.appendChild(container);
    }
    while (container.children.length >= 3) {
        container.removeChild(container.firstChild);
    }
    var toast = document.createElement('div');
    toast.className = 'alert alert-' + type + ' alert-dismissible fade show mb-0';
    toast.setAttribute('role', 'alert');
    toast.style.boxShadow = 'var(--p2m-shadow-3, 0 8px 24px rgba(0,0,0,0.15))';
    toast.textContent = message;
    // Phase 3: notification toasts carry a View link to the target.
    if (linkUrl) {
        var link = document.createElement('a');
        link.href = linkUrl;
        link.className = 'alert-link ms-2';
        link.textContent = linkLabel || 'View';
        toast.appendChild(link);
    }
    var close = document.createElement('button');
    close.type = 'button';
    close.className = 'btn-close';
    close.setAttribute('data-bs-dismiss', 'alert');
    close.setAttribute('aria-label', 'Close');
    toast.appendChild(close);
    container.appendChild(toast);
    setTimeout(function () {
        if (toast.isConnected) {
            toast.classList.remove('show');
            setTimeout(function () { toast.remove(); }, 300);
        }
    }, 5000);
}

// ---------------------------------------------------------------- notifications (Phase 3)

function updateUnreadBadges(count) {
    // The desktop nav badge and the mobile tab-bar badge stay in sync.
    document.querySelectorAll('#nav-notifications-badge, .tab-badge').forEach(function (badge) {
        if (count > 0) {
            badge.textContent = count <= 99 ? count : '99+';
            badge.style.display = '';
        } else {
            badge.style.display = 'none';
        }
    });
}

function notificationIconSvg(name, size) {
    // Same markup as the icon() macro; the name comes from the
    // server's fixed allowlist (utils.notifications.NOTIFICATION_ICONS).
    return '<svg class="lucide-icon" width="' + size + '" height="' + size
        + '" aria-hidden="true" focusable="false"><use href="/static/img/icons.svg#icon-'
        + name + '"></use></svg>';
}

function notificationNode(item) {
    var row = document.createElement('a');
    row.href = (item.link_url && item.link_url.startsWith('/'))
        ? item.link_url : '/social/notifications';
    row.className = 'notification-item' + (item.is_read ? '' : ' is-unread');
    if (!item.is_read) {
        var dot = document.createElement('span');
        dot.className = 'notification-dot';
        dot.setAttribute('aria-hidden', 'true');
        row.appendChild(dot);
    }
    var iconWrap = document.createElement('span');
    iconWrap.className = 'notification-item-icon';
    if (item.actor_avatar_url) {
        var img = document.createElement('img');
        img.src = item.actor_avatar_url;
        img.alt = '';
        img.className = 'avatar avatar-36';
        iconWrap.appendChild(img);
    } else if (item.actor_name) {
        var ph = document.createElement('span');
        ph.className = 'avatar-placeholder avatar-36';
        ph.setAttribute('aria-hidden', 'true');
        ph.textContent = item.actor_name.charAt(0).toUpperCase();
        iconWrap.appendChild(ph);
    } else {
        var typeIcon = document.createElement('span');
        typeIcon.className = 'notification-type-icon';
        typeIcon.innerHTML = notificationIconSvg(item.icon || 'bell', 18);
        iconWrap.appendChild(typeIcon);
    }
    row.appendChild(iconWrap);
    var main = document.createElement('span');
    main.className = 'notification-item-main';
    var title = document.createElement('span');
    title.className = 'notification-item-title';
    title.textContent = item.title;
    main.appendChild(title);
    if (item.body) {
        var body = document.createElement('span');
        body.className = 'notification-item-body';
        body.textContent = item.body;
        main.appendChild(body);
    }
    var time = document.createElement('span');
    time.className = 'notification-item-time';
    time.textContent = relativeTime(item.created_at);
    main.appendChild(time);
    row.appendChild(main);
    return row;
}

function renderPanelNotifications(bodyEl, items) {
    bodyEl.innerHTML = '';
    if (!items.length) {
        var empty = document.createElement('p');
        empty.className = 'notification-empty text-center py-4 mb-0';
        empty.textContent = 'No notifications yet.';
        bodyEl.appendChild(empty);
        return;
    }
    var unread = items.filter(function (n) { return !n.is_read; });
    var read = items.filter(function (n) { return n.is_read; });
    if (unread.length) {
        var newLabel = document.createElement('div');
        newLabel.className = 'notification-group-label';
        newLabel.textContent = 'New';
        bodyEl.appendChild(newLabel);
        unread.forEach(function (n) { bodyEl.appendChild(notificationNode(n)); });
    }
    if (read.length) {
        var earlierLabel = document.createElement('div');
        earlierLabel.className = 'notification-group-label';
        earlierLabel.textContent = 'Earlier';
        bodyEl.appendChild(earlierLabel);
        read.forEach(function (n) { bodyEl.appendChild(notificationNode(n)); });
    }
}

function renderPanelSkeletons(bodyEl, count) {
    bodyEl.innerHTML = '';
    for (var i = 0; i < count; i++) {
        var row = document.createElement('div');
        row.className = 'skeleton-notification';
        row.setAttribute('aria-hidden', 'true');
        var avatar = document.createElement('div');
        avatar.className = 'skeleton skeleton-avatar';
        var lines = document.createElement('div');
        lines.className = 'flex-grow-1';
        var line1 = document.createElement('div');
        line1.className = 'skeleton skeleton-line';
        line1.style.width = '80%';
        var line2 = document.createElement('div');
        line2.className = 'skeleton skeleton-line';
        line2.style.width = '55%';
        lines.appendChild(line1);
        lines.appendChild(line2);
        row.appendChild(avatar);
        row.appendChild(lines);
        bodyEl.appendChild(row);
    }
}

// ---------------------------------------------------------------- skeletons (Phase 3)

function skeletonLine(width) {
    var line = document.createElement('div');
    line.className = 'skeleton skeleton-line';
    line.style.width = width;
    return line;
}

function skeletonPostCard() {
    // A shimmering placeholder post, shown at the top of the feed
    // while a composer submit is in flight (same shape as the
    // skeleton_post macro in partials/skeleton.html).
    var card = document.createElement('div');
    card.className = 'skeleton-post';
    card.setAttribute('aria-hidden', 'true');
    var header = document.createElement('div');
    header.className = 'skeleton-post-header';
    var avatar = document.createElement('div');
    avatar.className = 'skeleton skeleton-avatar';
    var meta = document.createElement('div');
    meta.className = 'flex-grow-1';
    meta.appendChild(skeletonLine('40%'));
    meta.appendChild(skeletonLine('25%'));
    header.appendChild(avatar);
    header.appendChild(meta);
    card.appendChild(header);
    card.appendChild(skeletonLine('95%'));
    card.appendChild(skeletonLine('80%'));
    card.appendChild(skeletonLine('60%'));
    return card;
}

function skeletonCommentRow() {
    var row = document.createElement('div');
    row.className = 'skeleton-comment';
    row.setAttribute('aria-hidden', 'true');
    var avatar = document.createElement('div');
    avatar.className = 'skeleton skeleton-avatar-sm';
    var lines = document.createElement('div');
    lines.className = 'flex-grow-1';
    lines.appendChild(skeletonLine('70%'));
    lines.appendChild(skeletonLine('45%'));
    row.appendChild(avatar);
    row.appendChild(lines);
    return row;
}

function initNotificationsPanel() {
    var bell = document.getElementById('notifications-bell');
    var bodyEl = document.getElementById('notification-panel-body');
    if (!bell || !bodyEl) return;

    bell.addEventListener('show.bs.dropdown', function () {
        // Refresh on open (skeletons while fetching), then mark all
        // read — the badge clears on open, and the rendered rows keep
        // their unread dots for this viewing.
        renderPanelSkeletons(bodyEl, 3);
        apiRequest(bell.dataset.panelUrl, { method: 'GET' }).then(function (data) {
            renderPanelNotifications(bodyEl, data.notifications || []);
        }).catch(function () {
            // Keep the server-rendered items on failure.
            bodyEl.innerHTML = '';
            var msg = document.createElement('p');
            msg.className = 'notification-empty text-center py-4 mb-0';
            msg.textContent = 'Could not refresh notifications.';
            bodyEl.appendChild(msg);
        });
        apiRequest(bell.dataset.readAllUrl, { method: 'POST' }).then(function (data) {
            if (data.ok) updateUnreadBadges(0);
        }).catch(function () { /* badge stays — the page still works */ });
    });

    var markAll = document.getElementById('panel-mark-all-read');
    if (markAll) {
        markAll.addEventListener('click', function () {
            apiRequest(markAll.dataset.readAllUrl, { method: 'POST' }).then(function (data) {
                if (!data.ok) return;
                updateUnreadBadges(0);
                bodyEl.querySelectorAll('.notification-item.is-unread').forEach(function (item) {
                    item.classList.remove('is-unread');
                    var dot = item.querySelector('.notification-dot');
                    if (dot) dot.remove();
                });
            }).catch(function () { /* leave the rows as-is */ });
        });
    }
}

function initNotificationPoll() {
    var bell = document.getElementById('notifications-bell');
    if (!bell || !bell.dataset.pollUrl) return;
    var since = new Date().toISOString();
    var POLL_MS = 60000;
    setInterval(function () {
        if (document.hidden) return;
        apiRequest(bell.dataset.pollUrl + '?since=' + encodeURIComponent(since), { method: 'GET' })
            .then(function (data) {
                since = new Date().toISOString();
                updateUnreadBadges(data.unread || 0);
                (data.notifications || []).forEach(function (n) {
                    showToast(n.title, 'info', n.link_url, 'View');
                });
            })
            .catch(function () { /* next poll retries */ });
    }, POLL_MS);
}

// ---------------------------------------------------------------- relative time

function relativeTime(iso) {
    if (!iso) return '';
    var dt = new Date(iso);
    var seconds = Math.floor((Date.now() - dt.getTime()) / 1000);
    if (seconds < 60) return 'now';
    if (seconds < 3600) return Math.floor(seconds / 60) + 'm';
    if (seconds < 86400) return Math.floor(seconds / 3600) + 'h';
    if (seconds < 172800) return 'Yesterday';
    if (seconds < 604800) {
        return dt.toLocaleDateString(undefined, { weekday: 'short' });
    }
    return dt.toLocaleDateString(undefined, { month: 'short', day: 'numeric' });
}

// ---------------------------------------------------------------- post bodies

function initClamps(root) {
    root.querySelectorAll('[data-clamp]').forEach(function (body) {
        var card = body.closest('.post-card');
        var btn = card ? card.querySelector('.post-see-more') : null;
        if (!btn) return;
        body.classList.add('is-clamped');
        if (body.scrollHeight <= body.clientHeight + 1) {
            body.classList.remove('is-clamped');
            btn.hidden = true;
            return;
        }
        btn.hidden = false;
        btn.textContent = 'See more';
        btn.onclick = function () {
            var clamped = body.classList.toggle('is-clamped');
            btn.textContent = clamped ? 'See more' : 'See less';
        };
    });
}

// ---------------------------------------------------------------- composer

function initComposer() {
    var card = document.getElementById('composer');
    if (!card) return;
    var collapsed = document.getElementById('composer-collapsed');
    var form = document.getElementById('composer-form');
    var body = document.getElementById('composer-body');
    var submit = document.getElementById('composer-submit');
    var cancel = document.getElementById('composer-cancel');
    var typeInput = document.getElementById('composer-post-type');
    var bountyInput = document.getElementById('composer-bounty-id');
    var bountyFields = document.getElementById('composer-bounty-fields');
    var needKindRow = document.getElementById('composer-need-kind');
    var rewardWrap = document.getElementById('composer-reward-wrap');
    var titleInput = document.getElementById('composer-title');
    var rewardInput = document.getElementById('composer-reward');
    var attachBtn = document.getElementById('composer-attach-btn');
    var attachList = document.getElementById('composer-attach-list');
    var attachedLabel = document.getElementById('composer-attached-label');
    var pollBtn = document.getElementById('composer-poll-btn');
    var pollArea = document.getElementById('composer-poll-area');
    var mode = 'update';
    var needKind = 'free';

    function expand() {
        card.classList.add('is-expanded');
        body.focus();
    }

    function collapse() {
        card.classList.remove('is-expanded');
        form.reset();
        setMode('update');
        clearAttach();
        if (attachList) attachList.classList.remove('is-visible');
        if (pollArea) pollArea.hidden = true;
        body.style.height = 'auto';
        validate();
    }

    function setMode(next) {
        mode = next;
        card.querySelectorAll('[data-post-type]').forEach(function (chip) {
            chip.classList.toggle(
                'is-active', chip.getAttribute('data-post-type') === next
            );
        });
        var isNeed = next === 'need';
        bountyFields.classList.toggle('is-visible', isNeed);
        needKindRow.hidden = !isNeed;
        if (attachBtn) attachBtn.style.display = isNeed ? 'none' : '';
        if (isNeed) clearAttach();
        // #intros prompt: an intro post starts with the tag, so new
        // users never face a blank box.
        if (next === 'intro' && body.value.trim().length === 0) {
            body.value = '#intros ';
            body.dispatchEvent(new Event('input'));
        }
        typeInput.value = isNeed
            ? (needKind === 'paid' ? 'need_paid' : 'need_free')
            : next;
        validate();
    }

    function setNeedKind(kind) {
        needKind = kind;
        needKindRow.querySelectorAll('[data-need-kind]').forEach(function (chip) {
            chip.classList.toggle(
                'is-active', chip.getAttribute('data-need-kind') === kind
            );
        });
        rewardWrap.hidden = kind !== 'paid';
        typeInput.value = kind === 'paid' ? 'need_paid' : 'need_free';
        validate();
    }

    function clearAttach() {
        bountyInput.value = '';
        if (attachedLabel) attachedLabel.textContent = '';
    }

    function validate() {
        var ok = body.value.trim().length > 0;
        if (mode === 'need') {
            ok = ok && titleInput.value.trim().length >= 10;
            if (needKind === 'paid') {
                // Phase 3 bounty floor: paid needs state at least $50
                // (MIN_PAID_BOUNTY_CAD); the server enforces it too.
                ok = ok && Number(rewardInput.value) >= 50;
            }
        }
        submit.disabled = !ok;
    }

    collapsed.addEventListener('click', expand);
    collapsed.addEventListener('keydown', function (e) {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); expand(); }
    });
    cancel.addEventListener('click', collapse);

    card.querySelectorAll('[data-post-type]').forEach(function (chip) {
        chip.addEventListener('click', function () {
            setMode(chip.getAttribute('data-post-type'));
        });
    });
    needKindRow.querySelectorAll('[data-need-kind]').forEach(function (chip) {
        chip.addEventListener('click', function () {
            setNeedKind(chip.getAttribute('data-need-kind'));
        });
    });

    body.addEventListener('input', function () {
        body.style.height = 'auto';
        body.style.height = body.scrollHeight + 'px';
        validate();
    });
    [titleInput, rewardInput].forEach(function (input) {
        if (input) input.addEventListener('input', validate);
    });

    if (attachBtn && attachList) {
        attachBtn.addEventListener('click', function () {
            attachList.classList.toggle('is-visible');
        });
        attachList.querySelectorAll('[data-bounty-id]').forEach(function (item) {
            item.addEventListener('click', function () {
                bountyInput.value = item.getAttribute('data-bounty-id');
                if (attachedLabel) {
                    attachedLabel.textContent = 'Attached: '
                        + item.getAttribute('data-bounty-title');
                }
                attachList.classList.remove('is-visible');
            });
        });
    }

    if (pollBtn && pollArea) {
        pollBtn.addEventListener('click', function () {
            pollArea.hidden = !pollArea.hidden;
            if (!pollArea.hidden) {
                var area = document.getElementById('composer-poll-options');
                if (area) area.focus();
            }
        });
    }

    form.addEventListener('submit', function (e) {
        e.preventDefault();
        submit.disabled = true;
        // Phase 3 skeleton: a placeholder post at the top of the
        // feed while the submit is in flight.
        var list = document.getElementById('feed-list');
        var skeleton = null;
        if (list) {
            skeleton = skeletonPostCard();
            var empty = list.querySelector('.alert-light');
            if (empty) empty.remove();
            list.prepend(skeleton);
        }
        apiRequest(form.getAttribute('data-post-url'), {
            method: 'POST',
            body: new FormData(form),
        }).then(function (data) {
            if (data.redirect) {
                window.location.href = data.redirect;
                return;
            }
            if (skeleton) skeleton.remove();
            if (list && data.html) {
                var wrapper = document.createElement('div');
                wrapper.innerHTML = data.html.trim();
                var newCard = wrapper.firstChild;
                list.prepend(newCard);
                initClamps(newCard);
                window.scrollTo({ top: 0, behavior: 'smooth' });
            }
            collapse();
            showToast('Posted!');
        }).catch(function (err) {
            if (skeleton) skeleton.remove();
            if (err.message !== 'auth') {
                showToast(err.message || 'Could not post — try again.', 'danger');
            }
            validate();
        });
    });

    // The nav Post button / bottom tab link to #composer: expand it.
    if (window.location.hash === '#composer') {
        expand();
        card.scrollIntoView({ behavior: 'smooth', block: 'start' });
    }
}

// ---------------------------------------------------------------- new-posts pill

function initNewPostsPill() {
    var pill = document.getElementById('new-posts-pill');
    if (!pill) return;
    // Following tab only (Discover ranking makes "newer than" meaningless).
    var tab = new URLSearchParams(window.location.search).get('tab');
    if (tab && tab !== 'following') return;
    var since = pill.getAttribute('data-since');
    if (!since) return;
    var countEl = document.getElementById('new-posts-count');
    var count = 0;

    pill.addEventListener('click', function () {
        window.location.reload();
    });

    setInterval(function () {
        apiRequest(
            pill.getAttribute('data-new-count-url')
                + '?since=' + encodeURIComponent(since)
        ).then(function (data) {
            count = data.count || 0;
            if (count > 0 && window.scrollY > 300) {
                countEl.textContent = count;
                pill.classList.add('is-visible');
            }
        }).catch(function () { /* pill is best-effort — stay silent */ });
    }, 45000);
}

// ---------------------------------------------------------------- delegated actions

function cardOf(el) {
    return el.closest('.post-card');
}

function setCount(card, selector, n, singular, plural) {
    var el = card.querySelector(selector);
    if (el) el.textContent = n + ' ' + (n === 1 ? singular : plural);
}

function avatarNode(url, name) {
    if (url) {
        var img = document.createElement('img');
        img.src = url;
        img.alt = '';
        img.className = 'avatar avatar-32';
        return img;
    }
    var span = document.createElement('span');
    span.className = 'avatar-placeholder avatar-32';
    span.setAttribute('aria-hidden', 'true');
    span.textContent = (name || 'P')[0].toUpperCase();
    return span;
}

function commentNode(comment) {
    var row = document.createElement('div');
    row.className = 'post-comment-row';
    row.appendChild(avatarNode(comment.avatar_url, comment.author_name));
    var body = document.createElement('div');
    body.className = 'post-comment-body';
    var head = document.createElement('div');
    var link = document.createElement('a');
    link.href = comment.author_url;
    link.className = 'fw-bold text-decoration-none';
    link.textContent = comment.author_name;
    head.appendChild(link);
    var time = document.createElement('small');
    time.className = 'text-muted ms-2';
    time.textContent = relativeTime(comment.created_at);
    head.appendChild(time);
    body.appendChild(head);
    var text = document.createElement('div');
    text.textContent = comment.body;
    body.appendChild(text);
    if (comment.reply_count) {
        var replies = document.createElement('div');
        replies.className = 'text-muted small';
        replies.textContent = comment.reply_count
            + (comment.reply_count === 1 ? ' reply' : ' replies');
        body.appendChild(replies);
    }
    row.appendChild(body);
    return row;
}

// ---------------------------------------------------------------- polls

function renderPollResults(container, data) {
    // Replace the option buttons with the chart (bars + percentages).
    container.innerHTML = '';
    var results = document.createElement('div');
    results.className = 'poll-results';
    data.options.forEach(function (option) {
        var row = document.createElement('div');
        row.className = 'poll-result'
            + (option.id === data.voted_option_id ? ' is-voted' : '');
        var head = document.createElement('div');
        head.className = 'poll-result-head';
        var label = document.createElement('span');
        label.textContent = option.text;
        if (option.id === data.voted_option_id) {
            label.textContent += ' \u2713';
        }
        var stats = document.createElement('span');
        stats.textContent = option.percent + '% \u00b7 ' + option.votes
            + (option.votes === 1 ? ' vote' : ' votes');
        head.appendChild(label);
        head.appendChild(stats);
        var bar = document.createElement('div');
        bar.className = 'poll-result-bar';
        var fill = document.createElement('div');
        fill.className = 'poll-result-fill';
        fill.style.width = option.percent + '%';
        bar.appendChild(fill);
        row.appendChild(head);
        row.appendChild(bar);
        results.appendChild(row);
    });
    container.appendChild(results);
    var total = document.createElement('div');
    total.className = 'poll-total';
    total.textContent = data.total_votes
        + (data.total_votes === 1 ? ' vote' : ' votes');
    container.appendChild(total);
}

function initDelegatedActions() {
    // Likes — optimistic toggle, reconcile with the server.
    document.addEventListener('click', function (e) {
        var likeBtn = e.target.closest('.post-like-btn');
        if (likeBtn) {
            e.preventDefault();
            var card = cardOf(likeBtn);
            var liked = likeBtn.getAttribute('data-liked') === '1';
            var countEl = likeBtn.querySelector('.action-count');
            var count = parseInt(countEl.textContent, 10) || 0;
            var nextLiked = !liked;
            var nextCount = nextLiked ? count + 1 : Math.max(0, count - 1);
            likeBtn.classList.toggle('is-active', nextLiked);
            likeBtn.setAttribute('data-liked', nextLiked ? '1' : '0');
            countEl.textContent = nextCount;
            setCount(card, '[data-like-count]', nextCount, 'like', 'likes');
            if (nextLiked) {
                likeBtn.classList.add('pop');
                setTimeout(function () {
                    likeBtn.classList.remove('pop');
                }, 200);
            }
            apiRequest(likeBtn.getAttribute('data-like-url'), { method: 'POST' })
                .then(function (data) {
                    likeBtn.classList.toggle('is-active', data.liked);
                    likeBtn.setAttribute(
                        'data-liked', data.liked ? '1' : '0'
                    );
                    countEl.textContent = data.like_count;
                    setCount(
                        card, '[data-like-count]',
                        data.like_count, 'like', 'likes'
                    );
                })
                .catch(function (err) {
                    if (err.message === 'auth') return;
                    likeBtn.classList.toggle('is-active', liked);
                    likeBtn.setAttribute('data-liked', liked ? '1' : '0');
                    countEl.textContent = count;
                    setCount(card, '[data-like-count]', count, 'like', 'likes');
                    showToast('Could not update the like — try again.', 'danger');
                });
            return;
        }

        // Comment toggle — expand inline, lazy-load the latest comments.
        var commentBtn = e.target.closest('.post-comment-btn');
        if (commentBtn) {
            e.preventDefault();
            var card = cardOf(commentBtn);
            var box = card.querySelector('.post-comments-inline');
            var open = box.classList.toggle('is-visible');
            if (!open || box.getAttribute('data-loaded')) {
                if (open) {
                    var input = box.querySelector('input[name="body"]');
                    if (input) input.focus();
                }
                return;
            }
            box.setAttribute('data-loaded', '1');
            // Phase 3 skeleton: placeholder comment rows while loading.
            var commentList = box.querySelector('.post-comments-list');
            var commentSkeletons = [
                skeletonCommentRow(), skeletonCommentRow(),
            ];
            commentSkeletons.forEach(function (s) { commentList.appendChild(s); });
            apiRequest(commentBtn.getAttribute('data-comments-url'))
                .then(function (data) {
                    commentSkeletons.forEach(function (s) { s.remove(); });
                    var list = box.querySelector('.post-comments-list');
                    data.comments.forEach(function (c) {
                        list.appendChild(commentNode(c));
                    });
                    if (!data.comments.length) {
                        var empty = document.createElement('p');
                        empty.className = 'text-muted small mb-2';
                        empty.textContent = 'No comments yet — be the first.';
                        list.appendChild(empty);
                    }
                    var input = box.querySelector('input[name="body"]');
                    if (input) input.focus();
                })
                .catch(function () {
                    commentSkeletons.forEach(function (s) { s.remove(); });
                    box.removeAttribute('data-loaded');
                    showToast('Could not load comments.', 'danger');
                });
            return;
        }

        // Save — optimistic toggle, reconcile with the server.
        var saveBtn = e.target.closest('.post-save-btn');
        if (saveBtn) {
            e.preventDefault();
            var saved = saveBtn.getAttribute('data-saved') === '1';
            saveBtn.classList.toggle('is-active', !saved);
            saveBtn.setAttribute('data-saved', saved ? '0' : '1');
            if (!saved) {
                // Phase 3 motion: save fill + tiny rotate-settle.
                saveBtn.classList.add('settle');
                setTimeout(function () {
                    saveBtn.classList.remove('settle');
                }, 200);
            }
            apiRequest(saveBtn.getAttribute('data-save-url'), { method: 'POST' })
                .then(function (data) {
                    saveBtn.classList.toggle('is-active', data.saved);
                    saveBtn.setAttribute('data-saved', data.saved ? '1' : '0');
                    showToast(data.saved ? 'Saved.' : 'Removed from saved.');
                })
                .catch(function (err) {
                    if (err.message === 'auth') return;
                    saveBtn.classList.toggle('is-active', saved);
                    saveBtn.setAttribute('data-saved', saved ? '1' : '0');
                    showToast('Could not save — try again.', 'danger');
                });
            return;
        }

        // Onboarding dismiss — hides the checklist for this session.
        var dismissBtn = e.target.closest('.onboarding-dismiss');
        if (dismissBtn) {
            e.preventDefault();
            var card = document.getElementById('onboarding-card');
            apiRequest(
                dismissBtn.getAttribute('data-dismiss-url'), { method: 'POST' }
            ).catch(function () { /* dismissal is best-effort */ });
            if (card) card.remove();
            return;
        }

        // Poll vote — one vote per user; the server returns the chart.
        var pollBtn = e.target.closest('.poll-option-btn');
        if (pollBtn && !pollBtn.classList.contains('disabled')) {
            e.preventDefault();
            var poll = pollBtn.closest('.post-poll');
            var formData = new FormData();
            formData.append(
                'option_id', pollBtn.getAttribute('data-option-id')
            );
            poll.querySelectorAll('.poll-option-btn').forEach(function (b) {
                b.disabled = true;
            });
            apiRequest(poll.getAttribute('data-vote-url'), {
                method: 'POST',
                body: formData,
            }).then(function (data) {
                renderPollResults(poll, data);
            }).catch(function (err) {
                if (err.message === 'auth') return;
                poll.querySelectorAll('.poll-option-btn').forEach(function (b) {
                    b.disabled = false;
                });
                showToast(err.message || 'Could not vote.', 'danger');
            });
            return;
        }

        // Follow pills — optimistic toggle.
        var followBtn = e.target.closest('.btn-follow');
        if (followBtn) {
            e.preventDefault();
            var following = followBtn.getAttribute('data-following') === '1';
            var url = following
                ? followBtn.getAttribute('data-unfollow-url')
                : followBtn.getAttribute('data-follow-url');
            followBtn.classList.toggle('is-following', !following);
            followBtn.setAttribute('data-following', following ? '0' : '1');
            apiRequest(url, { method: 'POST' })
                .then(function (data) {
                    followBtn.classList.toggle('is-following', data.following);
                    followBtn.setAttribute(
                        'data-following', data.following ? '1' : '0'
                    );
                    document.querySelectorAll('[data-followers-count]')
                        .forEach(function (el) {
                            el.textContent = data.followers_count;
                        });
                })
                .catch(function (err) {
                    if (err.message === 'auth') return;
                    followBtn.classList.toggle('is-following', following);
                    followBtn.setAttribute(
                        'data-following', following ? '1' : '0'
                    );
                    showToast(err.message || 'Could not update follow.', 'danger');
                });
            return;
        }

        // Copy link.
        var copyBtn = e.target.closest('.post-copy-link');
        if (copyBtn) {
            e.preventDefault();
            var link = copyBtn.getAttribute('data-copy-link');
            var done = function () { showToast('Link copied.'); };
            if (navigator.clipboard && navigator.clipboard.writeText) {
                navigator.clipboard.writeText(link).then(done).catch(function () {
                    showToast('Could not copy the link.', 'danger');
                });
            } else {
                var ta = document.createElement('textarea');
                ta.value = link;
                document.body.appendChild(ta);
                ta.select();
                try { document.execCommand('copy'); done(); }
                catch (err) { showToast('Could not copy the link.', 'danger'); }
                ta.remove();
            }
            return;
        }

        // Mute author — removes the card from this feed.
        var muteBtn = e.target.closest('.post-mute-btn');
        if (muteBtn) {
            e.preventDefault();
            var name = muteBtn.getAttribute('data-author-name') || 'this author';
            if (!window.confirm('Mute ' + name + '? You will not see their posts.')) return;
            var card = cardOf(muteBtn);
            apiRequest(muteBtn.getAttribute('data-mute-url'), { method: 'POST' })
                .then(function () {
                    card.style.transition = 'opacity 180ms ease-out';
                    card.style.opacity = '0';
                    setTimeout(function () { card.remove(); }, 200);
                    showToast('Muted ' + name + '.');
                })
                .catch(function (err) {
                    if (err.message !== 'auth') {
                        showToast(err.message || 'Could not mute.', 'danger');
                    }
                });
            return;
        }

        // Delete own post.
        var deleteBtn = e.target.closest('.post-delete-btn');
        if (deleteBtn) {
            e.preventDefault();
            if (!window.confirm('Delete this post?')) return;
            var card = cardOf(deleteBtn);
            apiRequest(deleteBtn.getAttribute('data-delete-url'), { method: 'POST' })
                .then(function () {
                    card.remove();
                    showToast('Post deleted.');
                })
                .catch(function (err) {
                    if (err.message !== 'auth') {
                        showToast(err.message || 'Could not delete.', 'danger');
                    }
                });
            return;
        }
    });

    // Inline comment submit.
    document.addEventListener('submit', function (e) {
        var commentForm = e.target.closest('.post-comment-form');
        if (commentForm) {
            e.preventDefault();
            var card = cardOf(commentForm);
            var input = commentForm.querySelector('input[name="body"]');
            var text = input.value.trim();
            if (!text) return;
            var btn = commentForm.querySelector('button[type="submit"]');
            btn.disabled = true;
            apiRequest(commentForm.getAttribute('data-comment-url'), {
                method: 'POST',
                body: new FormData(commentForm),
            }).then(function (data) {
                var list = card.querySelector('.post-comments-list');
                var empty = list.querySelector('p.text-muted');
                if (empty) empty.remove();
                list.appendChild(commentNode(data.comment));
                setCount(
                    card, '[data-comment-count]',
                    data.comment_count, 'comment', 'comments'
                );
                var actionCount = card.querySelector(
                    '.post-comment-btn .action-count'
                );
                if (actionCount) actionCount.textContent = data.comment_count;
                input.value = '';
            }).catch(function (err) {
                if (err.message !== 'auth') {
                    showToast(err.message || 'Could not post the comment.', 'danger');
                }
            }).finally(function () {
                btn.disabled = false;
            });
            return;
        }

        // Reshare submit.
        var shareForm = e.target.closest('.post-share-form');
        if (shareForm) {
            e.preventDefault();
            var card = cardOf(shareForm);
            var btn = shareForm.querySelector('button[type="submit"]');
            btn.disabled = true;
            apiRequest(shareForm.getAttribute('data-share-url'), {
                method: 'POST',
                body: new FormData(shareForm),
            }).then(function (data) {
                var shareBtn = card.querySelector('.post-share-btn');
                if (shareBtn) {
                    shareBtn.classList.add('is-active');
                    var countEl = shareBtn.querySelector('.action-count');
                    if (countEl) countEl.textContent = data.share_count;
                }
                var collapse = shareForm.closest('.collapse');
                if (collapse) {
                    var instance = window.bootstrap
                        && window.bootstrap.Collapse.getInstance(collapse);
                    if (instance) instance.hide();
                    else collapse.classList.remove('show');
                }
                showToast('Shared to your feed!');
            }).catch(function (err) {
                if (err.message !== 'auth') {
                    showToast(err.message || 'Could not share.', 'danger');
                }
            }).finally(function () {
                btn.disabled = false;
            });
            return;
        }
    });
}

// ---------------------------------------------------------------- tab indicator (Phase 3)

function initTabIndicator() {
    // A primary bar that slides under the active tab (180ms, see
    // .tab-indicator in main.css). Works for link tabs (feed — the
    // page reloads, so position-on-load is enough) and Bootstrap tab
    // panes (profile tabs — reposition on shown.bs.tab).
    document.querySelectorAll('.nav-tabs').forEach(function (tabs) {
        var indicator = document.createElement('span');
        indicator.className = 'tab-indicator';
        indicator.setAttribute('aria-hidden', 'true');
        tabs.appendChild(indicator);
        var position = function () {
            var active = tabs.querySelector('.nav-link.active');
            if (!active) {
                indicator.style.opacity = '0';
                return;
            }
            indicator.style.opacity = '1';
            indicator.style.width = active.offsetWidth + 'px';
            indicator.style.transform = 'translateX(' + active.offsetLeft + 'px)';
        };
        position();
        window.addEventListener('resize', position);
        document.addEventListener('shown.bs.tab', position);
        tabs.addEventListener('click', function () {
            setTimeout(position, 0);
        });
    });
}
