"""Narrated training videos, reachable from the page they explain.

James 2026-09-23: buyers (Andrew, Cheran) should be able to open a
walkthrough of any Buying page at any time. Videos are screen
recordings with voiceover + burned-in captions, stored in
``training_videos/`` and served through ``st.video`` (so they sit behind
the app login, unlike ``/app/static``). Add a page by dropping the MP4 in
that folder and adding a row to ``VIDEOS``.
"""

from __future__ import annotations

from pathlib import Path

import streamlit as st

VIDEO_DIR = Path(__file__).resolve().parent.parent / "training_videos"

# page name -> video metadata. Order = order in the sidebar list.
VIDEOS: dict[str, dict] = {
    "Buying Priority": {
        "file": "buying_priority.mp4",
        "title": "Buying Priority: what to buy today",
        "length": "2½ min",
        "recorded": "2026-09-23",
    },
}

_OPEN_KEY = "_training_video_open_{page}"


def _path(page: str) -> Path | None:
    meta = VIDEOS.get(page)
    if not meta:
        return None
    p = VIDEO_DIR / meta["file"]
    return p if p.is_file() else None


def available_videos() -> list[str]:
    """Pages that have a video file on disk."""
    return [p for p in VIDEOS if _path(p) is not None]


def _open_video_on(page: str, nav_key: str = "_nav_request") -> None:
    """Sidebar callback: go to `page` and open its video."""
    st.session_state[nav_key] = {"page": page}
    st.session_state[_OPEN_KEY.format(page=page)] = True


def render_training_button(page: str) -> None:
    """Top-of-page toggle; the video only loads when it is switched on."""
    path = _path(page)
    if path is None:
        return
    meta = VIDEOS[page]
    key = _OPEN_KEY.format(page=page)
    on = st.toggle(f"🎓 Training video ({meta['length']})", key=key,
                   help=meta["title"])
    if on:
        with st.container(border=True):
            st.video(str(path))
            st.caption(f"{meta['title']} · recorded {meta['recorded']}. "
                       "Switch the toggle off to hide it.")


def render_sidebar_list(visible_pages: list[str]) -> None:
    """Sidebar expander listing every video the user can open."""
    pages = [p for p in available_videos() if p in visible_pages]
    if not pages:
        return
    with st.expander(f"🎓 Training videos ({len(pages)})", expanded=False):
        for p in pages:
            meta = VIDEOS[p]
            st.button(f"▶ {p} · {meta['length']}", key=f"_tv_nav_{p}",
                      on_click=_open_video_on, args=(p,),
                      help=meta["title"], width="stretch")
