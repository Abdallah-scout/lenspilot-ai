"""Published portfolio: reads ONLY records actually written to sandbox/published. Owner: Abdallah."""

from __future__ import annotations

import html

import streamlit as st

from ui.common import get_services


def render() -> None:
    sb = get_services().sandbox
    records = sb.published_records()
    st.title("Published portfolio")
    st.markdown('<p class="lp-lede">What the agent has written to the sandbox after your approval. '
                "This page reads the sandbox files directly, so it stays correct after a refresh or restart.</p>",
                unsafe_allow_html=True)
    if not records:
        st.info("Nothing is published yet. Ask the agent to prepare a collection, then approve it.")
        return
    collections = sorted({r["collection"] for r in records})
    for coll in collections:
        items = [r for r in records if r["collection"] == coll]
        st.subheader(f"{coll.capitalize()} ({len(items)})")
        cols = st.columns(3)
        for i, r in enumerate(items):
            with cols[i % 3]:
                st.image(str(sb.published_dir / r["image_file"]), width="stretch")
                st.markdown(f'<div class="lp-edge"><span>{r["photo_id"]}</span>'
                            f'<span>{r["published_at"][:10]}</span></div>'
                            f'<div class="lp-title">{html.escape(r["title"])}</div>'
                            f'<p class="lp-caption">{html.escape(r["caption"])}</p>'
                            f'<div class="lp-muted">Approved by a human in {r["run_id"]}. '
                            f'Key {r["publication_key"][:10]}.</div>', unsafe_allow_html=True)
                st.write("")
