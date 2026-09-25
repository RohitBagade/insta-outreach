"""A small mock of the Instagram web UI for exercising the real browser agent.

It mirrors the *semantics* the agent relies on — URL shapes, <meta> tags,
ARIA roles/labels, dialogs, a contenteditable composer — not Instagram's
markup. Fault modes reproduce barriers (login wall, checkpoint, "Try Again
Later", restrictions), benign pop-ups, UI drift and failed sends.

Validating selectors against the *real* Instagram UI is done with the
read-only ``insta-outreach browser probe`` command on Rohit's machine.
"""

from __future__ import annotations

import html
import itertools
import json
import threading
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import quote

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

FAULTS = {
    "login",
    "checkpoint",
    "rate_limit_dialog",
    "restricted_dialog",
    "notifications_dialog",
    "ui_changed",
    "send_fails",
    "request_pending",
}


@dataclass
class MockPost:
    shortcode: str
    caption: str
    date: str  # "September 20, 2026"
    likes: int = 42


@dataclass
class MockProfile:
    username: str
    full_name: str
    category: str | None = None
    bio: str = ""
    website: str | None = None
    followers: int = 1000
    following: int = 200
    posts: list[MockPost] = field(default_factory=list)
    private: bool = False
    can_message: bool = True
    similar: list[str] = field(default_factory=list)
    followers_list: list[str] = field(default_factory=list)


@dataclass
class MockThread:
    id: str
    username: str
    messages: list[tuple[str, str]] = field(default_factory=list)  # (in|out, text)


class MockState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.reset()

    def reset(self, prefix: str = "sim.") -> None:
        self.prefix = prefix
        self.profiles: dict[str, MockProfile] = {}
        self.threads: dict[str, MockThread] = {}
        self.faults: set[str] = set()
        self.sent: list[dict[str, str]] = []
        self.notifications_dismissed = False
        self._ids = itertools.count(1000)
        self.me = "lemmedeliver"
        seed(self, prefix)

    def thread_for(self, username: str, create: bool = True) -> MockThread | None:
        thread = self.threads.get(username)
        if thread is None and create:
            thread = MockThread(id=str(next(self._ids)), username=username)
            self.threads[username] = thread
        return thread

    def thread_by_id(self, thread_id: str) -> MockThread | None:
        return next((t for t in self.threads.values() if t.id == thread_id), None)


def seed(state: MockState, prefix: str = "sim.") -> None:
    """Fictional accounts. ``prefix`` lets LIVE-environment tests use non-``sim.`` handles."""
    posts = [
        MockPost(f"CAFE{i:03d}", f"Fresh cold brew batch #{i} #thanecafe #coffee", f"September {20 - i}, 2026")
        for i in range(6)
    ]
    state.profiles = {
        f"{prefix}the.brew.room": MockProfile(
            f"{prefix}the.brew.room",
            "The Brew Room",
            "Cafe",
            "Specialty coffee & all-day breakfast in Thane\nReservations: DM us",
            None,
            6400,
            310,
            posts,
            similar=[f"{prefix}chai.and.chapters", f"{prefix}cake.canvas.powai"],
            followers_list=[f"{prefix}priya.travels", f"{prefix}chai.and.chapters", f"{prefix}tiny.bakes.dadar"],
        ),
        f"{prefix}smileline.dental": MockProfile(
            f"{prefix}smileline.dental",
            "Smileline Dental Studio",
            "Dentist",
            "Family & cosmetic dentistry in Andheri West\nDM to book your appointment",
            "https://smileline.example/",
            2400,
            180,
            [MockPost("DENT001", "Smile makeover #mumbaidentist", "September 18, 2026")],
        ),
        f"{prefix}chai.and.chapters": MockProfile(
            f"{prefix}chai.and.chapters",
            "Chai & Chapters",
            "Cafe",
            "Bookstore cafe in Vile Parle",
            None,
            4100,
            520,
            [MockPost("CHAI001", "Book club night #thanecafe", "September 15, 2026")],
        ),
        f"{prefix}cake.canvas.powai": MockProfile(
            f"{prefix}cake.canvas.powai",
            "Cake Canvas",
            "Bakery",
            "Custom cakes in Powai",
            None,
            2600,
            90,
            [MockPost("CAKE001", "Birthday cake #thanecafe", "September 12, 2026")],
            can_message=False,
        ),
        f"{prefix}private.nails": MockProfile(
            f"{prefix}private.nails",
            "Nail Art by Riya",
            "Nail Salon",
            "Nails in Vashi",
            None,
            1900,
            300,
            [],
            private=True,
        ),
    }
    # A thread Rohit started by hand before automation existed.
    chai = state.thread_for(f"{prefix}chai.and.chapters")
    assert chai is not None
    chai.messages.append(("out", "Hi! Loved your book club evenings, do you host private events?"))


STATE = MockState()


def _page(
    title: str,
    body: str,
    *,
    meta: dict[str, str] | None = None,
    extra_head: str = "",
    script: str = "",
    dialog: str = "",
    body_attrs: str = "",
) -> HTMLResponse:
    metas = "".join(
        f'<meta property="{k}" content="{html.escape(v)}">'
        if k.startswith("og:")
        else f'<meta name="{k}" content="{html.escape(v)}">'
        for k, v in (meta or {}).items()
    )
    ui_changed = "ui_changed" in STATE.faults
    search_label = "Explore people" if ui_changed else "Search"
    nav = f"""<nav>
      <a href="/" aria-label="Home"><svg aria-label="Home" width="10" height="10"></svg>Home</a>
      <a href="#" role="link" id="nav-search"><svg aria-label="{search_label}" width="10" height="10"></svg>{search_label}</a>
      <a href="/direct/inbox/">Messages</a>
      <a href="/{STATE.me}/">Profile</a>
    </nav>
    <div id="search-panel" hidden><input aria-label="Search input" placeholder="Search" id="search-input">
      <div id="search-results"></div></div>"""
    return HTMLResponse(f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{html.escape(title)}</title>
{metas}{extra_head}
<style>body{{font-family:sans-serif;margin:0;display:flex}} nav{{width:180px;display:flex;flex-direction:column;gap:8px;padding:12px}}
main{{flex:1;padding:12px;max-width:800px}} [role=grid]{{display:flex;flex-direction:column;width:600px}}
[role=row]{{display:flex;margin:4px 0}} [role=row][data-side=out]{{justify-content:flex-end}}
[role=row] div{{padding:6px 10px;border-radius:14px;background:#eee;max-width:60%}}
[role=dialog]{{position:fixed;top:30%;left:30%;background:#fff;border:1px solid #333;padding:20px;z-index:10}}
[role=button]{{cursor:pointer;display:inline-block;padding:4px 10px;border:1px solid #999;margin:2px}}</style></head>
<body {body_attrs}>{nav}<main>{body}</main>{dialog}
<script>
document.getElementById('nav-search').addEventListener('click', (e) => {{ e.preventDefault();
  document.getElementById('search-panel').hidden = false; document.getElementById('search-input').focus(); }});
document.getElementById('search-input').addEventListener('input', async (e) => {{
  const r = await fetch('/__api/search?q=' + encodeURIComponent(e.target.value));
  const users = await r.json();
  document.getElementById('search-results').innerHTML = users.map(u =>
    `<a href="/${{u.username}}/"><span>${{u.username}}</span><span>${{u.full_name}}</span></a>`).join('<br>');
}});
document.querySelectorAll('[data-dismiss]').forEach(b => b.addEventListener('click', async () => {{
  await fetch('/__api/dismiss', {{method: 'POST'}}); b.closest('[role=dialog]').remove(); }}));
{script}
</script></body></html>""")


def _barrier(request: Request) -> Response | None:
    path = request.url.path
    if "login" in STATE.faults and not path.startswith("/accounts/login"):
        return RedirectResponse("/accounts/login/?next=" + quote(path), status_code=302)
    if "checkpoint" in STATE.faults and not path.startswith("/challenge"):
        return RedirectResponse("/challenge/?next=" + quote(path), status_code=302)
    return None


def _dialog() -> str:
    if "rate_limit_dialog" in STATE.faults:
        return (
            '<div role="dialog"><h3>Try Again Later</h3><p>We limit how often you can do certain things on '
            'Instagram to protect our community.</p><div role="button">OK</div></div>'
        )
    if "restricted_dialog" in STATE.faults:
        return (
            '<div role="dialog"><h3>Action Blocked</h3><p>We restrict certain activity to protect our '
            'community.</p><div role="button">Tell us</div></div>'
        )
    if "notifications_dialog" in STATE.faults and not STATE.notifications_dismissed:
        return (
            '<div role="dialog"><h3>Turn on Notifications</h3><p>Know right away when people follow you.</p>'
            '<button>Turn On</button><button data-dismiss="1">Not Now</button></div>'
        )
    return ""


async def home(request: Request) -> Response:
    if (barrier := _barrier(request)) is not None:
        return barrier
    p = STATE.prefix
    body = (
        f'<h1>Feed</h1><article><a href="/{p}feed.noise/">{p}feed.noise</a> posted a photo</article>'
        f'<article><a href="/{p}other.noise/">{p}other.noise</a> posted a reel</article>'
    )
    return _page("Instagram", body, dialog=_dialog())


def _header(profile: MockProfile) -> str:
    ui_changed = "ui_changed" in STATE.faults
    message_label = "Chat" if ui_changed else "Message"
    link = ""
    if profile.website:
        wrapped = "https://l.instagram.com/?u=" + quote(profile.website, safe="")
        link = f'<a href="{wrapped}" rel="nofollow">{html.escape(profile.website.split("//")[-1].rstrip("/"))}</a>'
    category = f"<div>{html.escape(profile.category)}</div>" if profile.category else ""
    bio = "".join(f"<span>{html.escape(line)}</span><br>" for line in profile.bio.splitlines())
    buttons = (
        "<button>Follow</button>"
        + (f'<div role="button" id="message-btn">{message_label}</div>' if profile.can_message else "")
        + '<div role="button" aria-label="Similar accounts" id="similar-btn">v</div>'
    )
    return f"""<header><h2>{profile.username}</h2>
      <ul><li><span>{len(profile.posts)} posts</span></li>
      <li><a href="/{profile.username}/followers/">{profile.followers} followers</a></li>
      <li><a href="/{profile.username}/following/">{profile.following} following</a></li></ul>
      <div>{html.escape(profile.full_name)}</div>{category}<div>{bio}</div>{link}<div>{buttons}</div></header>"""


def _profile_meta(profile: MockProfile) -> dict[str, str]:
    return {
        "og:title": f"{profile.full_name} (@{profile.username}) • Instagram photos and videos",
        "og:description": f"{profile.followers:,} Followers, {profile.following:,} Following, "
        f"{len(profile.posts)} Posts - See Instagram photos and videos from "
        f"{profile.full_name} (@{profile.username})",
    }


_PROFILE_JS = """
const mb = document.getElementById('message-btn');
if (mb) mb.addEventListener('click', async () => {
  const r = await fetch('/__api/open?username=' + encodeURIComponent(document.querySelector('header h2').innerText), {method: 'POST'});
  const data = await r.json(); location.href = '/direct/t/' + data.thread_id + '/'; });
const sb = document.getElementById('similar-btn');
if (sb) sb.addEventListener('click', () => { document.getElementById('similar').hidden = false; });
"""


async def profile(request: Request) -> Response:
    if (barrier := _barrier(request)) is not None:
        return barrier
    username = request.path_params["username"].lower()
    prof = STATE.profiles.get(username)
    if prof is None:
        return _page(
            "Page not found • Instagram",
            "<h2>Sorry, this page isn't available.</h2><p>The link you followed may be broken, "
            "or the page may have been removed.</p>",
        )
    if prof.private:
        grid = "<h2>This account is private</h2><p>Follow to see their photos and videos.</p>"
    else:
        grid = "".join(
            f'<a href="/p/{p.shortcode}/"><img alt="Photo by {html.escape(prof.full_name)} on '
            f'{p.date}. May be an image of coffee." width="50" height="50"></a>'
            for p in prof.posts
        )
    similar = "".join(f'<a href="/{u}/">{u}</a> ' for u in prof.similar)
    dialog = _dialog()
    view = request.path_params.get("view")
    if view in ("followers", "following") and not prof.private:
        names = prof.followers_list if view == "followers" else prof.similar
        items = "".join(f'<div><a href="/{u}/">{u}</a></div>' for u in names)
        dialog = (
            dialog
            or f'<div role="dialog"><h3>{view.title()}</h3><div style="max-height:100px;overflow:auto">{items}</div></div>'
        )
    body = f"{_header(prof)}<section id='similar' hidden>Suggested for you {similar}</section><div>{grid}</div>"
    return _page(
        f"{prof.full_name} (@{prof.username}) • Instagram photos and videos",
        body,
        meta=_profile_meta(prof),
        script=_PROFILE_JS,
        dialog=dialog,
    )


async def post(request: Request) -> Response:
    if (barrier := _barrier(request)) is not None:
        return barrier
    code = request.path_params["shortcode"]
    for prof in STATE.profiles.values():
        for p in prof.posts:
            if p.shortcode == code:
                desc = f'{p.likes} likes, 2 comments - {prof.username} on {p.date}: "{p.caption}"'
                body = (
                    f'<article><header><a href="/{prof.username}/">{prof.username}</a></header>'
                    f'<p>{html.escape(p.caption)}</p><time datetime="2026-09-20T10:00:00Z">{p.date}</time>'
                    f'<ul><li><a href="/{STATE.prefix}priya.travels/">{STATE.prefix}priya.travels</a> '
                    f"Looks great!</li></ul></article>"
                )
                return _page(
                    f"{prof.full_name} on Instagram",
                    body,
                    meta={"og:description": desc, "description": desc},
                    dialog=_dialog(),
                )
    return _page("Page not found", "<h2>Sorry, this page isn't available.</h2>")


async def tag(request: Request) -> Response:
    if (barrier := _barrier(request)) is not None:
        return barrier
    name = request.path_params["tag"].lower()
    links = [
        f'<a href="/p/{p.shortcode}/"><img alt="post" width="40" height="40"></a>'
        for prof in STATE.profiles.values()
        for p in prof.posts
        if f"#{name}" in p.caption.lower()
    ]
    return _page(f"#{name} hashtag on Instagram", f"<h1>#{name}</h1>" + "".join(links), dialog=_dialog())


async def inbox(request: Request) -> Response:
    if (barrier := _barrier(request)) is not None:
        return barrier
    rows = []
    for thread in STATE.threads.values():
        prof = STATE.profiles.get(thread.username)
        last = thread.messages[-1] if thread.messages else None
        preview = ("You: " if last and last[0] == "out" else "") + (last[1][:40] if last else "")
        rows.append(
            f'<a href="/direct/t/{thread.id}/"><div>{html.escape(prof.full_name if prof else thread.username)}</div>'
            f"<div>{html.escape(preview)}</div></a>"
        )
    return _page("Inbox • Direct", "<h1>Chats</h1>" + "".join(rows), dialog=_dialog())


_THREAD_JS = """
const box = document.getElementById('composer');
async function send() {
  const text = box.innerText.replace(/\\n$/, '');
  if (!text.trim()) return;
  await fetch('/__api/send', {method: 'POST', headers: {'content-type': 'application/json'},
    body: JSON.stringify({thread_id: document.body.dataset.thread, text})});
  box.innerText = '';
  const r = await fetch('/__api/thread/' + document.body.dataset.thread);
  document.getElementById('grid').innerHTML = await r.text();
}
box.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); send(); } });
document.getElementById('send').addEventListener('click', send);
"""


def _rows(thread: MockThread) -> str:
    return "".join(
        f'<div role="row" data-side="{d}"><div dir="auto">{html.escape(t).replace(chr(10), "<br>")}</div></div>'
        for d, t in thread.messages
    )


async def thread_page(request: Request) -> Response:
    if (barrier := _barrier(request)) is not None:
        return barrier
    thread = STATE.thread_by_id(request.path_params["thread_id"])
    if thread is None:
        return _page("Page not found", "<h2>Sorry, this page isn't available.</h2>")
    prof = STATE.profiles.get(thread.username)
    ui_changed = "ui_changed" in STATE.faults
    label = "Write something..." if ui_changed else "Message..."
    send_label = "Submit" if ui_changed else "Send"
    banner = ""
    if "request_pending" in STATE.faults and thread.messages:
        banner = "<p>You can send more messages after they accept your request.</p>"
    if prof is not None and not prof.can_message:
        banner = "<p>This account can't receive your messages.</p>"
    body = (
        f'<section><a href="/{thread.username}/">{html.escape(prof.full_name if prof else thread.username)}</a>'
        f"<span>{thread.username}</span></section>"
        f'<div role="grid" aria-label="Messages in conversation with {thread.username}" id="grid">{_rows(thread)}</div>'
        f'{banner}<div role="textbox" contenteditable="true" aria-label="{label}" id="composer"></div>'
        f'<div role="button" id="send">{send_label}</div>'
    )
    return _page(
        "Direct • Instagram", body, script=_THREAD_JS, dialog=_dialog(), body_attrs=f'data-thread="{thread.id}"'
    )


async def login_page(request: Request) -> Response:
    return _page(
        "Login • Instagram",
        '<form><input name="username" aria-label="Phone number, username, or email">'
        '<input name="password" type="password" aria-label="Password"><button>Log in</button></form>',
    )


async def challenge_page(request: Request) -> Response:
    return _page(
        "Security check",
        "<h2>Confirm it's you</h2><p>We detected an unusual login attempt. "
        "Enter the security code we sent to your email.</p><input aria-label='Security code'>",
    )


# -- JSON endpoints used by the page scripts ------------------------------------------------
async def api_search(request: Request) -> Response:
    q = request.query_params.get("q", "").lower()
    words = [w for w in q.split() if w]
    hits = [
        {"username": p.username, "full_name": p.full_name}
        for p in STATE.profiles.values()
        if words and any(w in f"{p.username} {p.full_name} {p.category or ''} {p.bio}".lower() for w in words)
    ]
    return JSONResponse(hits)


async def api_open(request: Request) -> Response:
    username = request.query_params["username"].lower()
    with STATE.lock:
        thread = STATE.thread_for(username)
    assert thread is not None
    return JSONResponse({"thread_id": thread.id})


async def api_send(request: Request) -> Response:
    payload = await request.json()
    thread = STATE.thread_by_id(str(payload["thread_id"]))
    if thread is None:
        return JSONResponse({"ok": False}, status_code=404)
    if "send_fails" in STATE.faults:
        return JSONResponse({"ok": False}, status_code=500)
    with STATE.lock:
        thread.messages.append(("out", payload["text"]))
        STATE.sent.append({"to": thread.username, "text": payload["text"]})
    return JSONResponse({"ok": True})


async def api_thread(request: Request) -> Response:
    thread = STATE.thread_by_id(request.path_params["thread_id"])
    return HTMLResponse(_rows(thread) if thread else "")


async def api_dismiss(request: Request) -> Response:
    STATE.notifications_dismissed = True
    return JSONResponse({"ok": True})


# -- test control -------------------------------------------------------------------------------
async def control(request: Request) -> Response:
    payload: dict[str, Any] = await request.json() if request.method == "POST" else {}
    with STATE.lock:
        if payload.get("reset"):
            STATE.reset(payload.get("prefix", "sim."))
        for fault in payload.get("add", []):
            if fault in FAULTS:
                STATE.faults.add(fault)
        for fault in payload.get("remove", []):
            STATE.faults.discard(fault)
        if "prospect_reply" in payload:
            thread = STATE.thread_for(payload["prospect_reply"]["username"])
            assert thread is not None
            thread.messages.append(("in", payload["prospect_reply"]["text"]))
        return JSONResponse(
            {
                "faults": sorted(STATE.faults),
                "sent": STATE.sent,
                "threads": {u: {"id": t.id, "messages": t.messages} for u, t in STATE.threads.items()},
            }
        )


def create_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/", home),
            Route("/accounts/login/", login_page),
            Route("/challenge/", challenge_page),
            Route("/direct/inbox/", inbox),
            Route("/direct/t/{thread_id}/", thread_page),
            Route("/explore/tags/{tag}/", tag),
            Route("/p/{shortcode}/", post),
            Route("/__api/search", api_search),
            Route("/__api/open", api_open, methods=["POST"]),
            Route("/__api/send", api_send, methods=["POST"]),
            Route("/__api/thread/{thread_id}", api_thread),
            Route("/__api/dismiss", api_dismiss, methods=["POST"]),
            Route("/__control", control, methods=["GET", "POST"]),
            Route("/{username}/", profile),
            Route("/{username}/{view}/", profile),
        ]
    )


def dumps_state() -> str:
    return json.dumps({"faults": sorted(STATE.faults), "sent": STATE.sent})
