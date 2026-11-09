"""Gallery view: the sandbox catalog as a contact sheet. Owner: Abdallah."""

from __future__ import annotations

import html

import streamlit as st

from ui.common import eligibility_tags, get_services, meter, quality_for


def render() -> None:
    sb = get_services().sandbox
    photos = sb.catalog()
    st.title("Contact sheet")
    st.markdown('<p class="lp-lede">Every photograph in the sandbox, with measured quality and '
                "publishing eligibility. Consent and visibility values are simulated demo data.</p>",
                unsafe_allow_html=True)

    c1, c2, c3 = st.columns([2, 2, 3])
    cats = sorted({p["category"] for p in photos})
    category = c1.segmented_control("Category", ["all", *cats], default="all")
    only_eligible = c2.toggle("Eligible only", value=False)
    sort_by = c3.segmented_control("Sort", ["catalog order", "quality"], default="catalog order")

    from security import check_publication_eligibility
    shown = [p for p in photos if category in (None, "all") or p["category"] == category]
    if only_eligible:
        shown = [p for p in shown if check_publication_eligibility(p).allowed]
    if sort_by == "quality":
        shown.sort(key=lambda p: (quality_for(p["photo_id"]) or {}).get("score", -1), reverse=True)
    if not shown:
        st.info("No photographs match these filters. Clear a filter to see the full sheet.")
        return

    cols = st.columns(3)
    for i, p in enumerate(shown):
        with cols[i % 3]:
            q = quality_for(p["photo_id"])
            st.image(str(sb.image_path(p["photo_id"])), width="stretch")
            score = f"{q['score']:.0f}" if q else "n/a"
            st.markdown(f'<div class="lp-edge"><span>{p["photo_id"]}</span><span>Q {score}</span></div>',
                        unsafe_allow_html=True)
            if q:
                st.markdown(meter(q["score"]), unsafe_allow_html=True)
            st.markdown(f'<div class="lp-title">{html.escape(p["title"])}</div>'
                        f'<div class="lp-muted">{html.escape(p["category"].capitalize())}, {html.escape(p["shoot"])}</div>'
                        f'<div style="margin:6px 0 4px">{eligibility_tags(p)}</div>', unsafe_allow_html=True)
            with st.expander("Metadata"):
                st.markdown(f'<div class="lp-muted">{p["camera"]}, {p["lens"]}<br>'
                            f'{p["focal_length_mm"]} mm, {p["aperture"]}, {p["shutter"]} s, ISO {p["iso"]}<br>'
                            f'Captured {p["captured_at"].replace("T", " ")}<br>'
                            f'Visibility {p["visibility"]}, consent {p["client_consent"]} (simulated)</div>',
                            unsafe_allow_html=True)
                if q:
                    st.caption(f"Sharpness {q['components']['sharpness']}, exposure "
                               f"{q['components']['exposure']}, contrast {q['components']['contrast']}. "
                               f"Estimator: {q['estimator']}.")
                st.caption("Description (untrusted dataset text):")
                st.text(p["description"])
            st.write("")
