"""Import page: choose photos from Google Photos, then decide what may be published.

Only a human can reach this page's actions. The agent has no import tool and
cannot change a photo's visibility or consent.
"""

from __future__ import annotations

import html

import streamlit as st

from importers import google_photos as gp
from importers.photo_import import ID_PREFIX, import_session
from importers.vision import VisionError, describe
from sandbox import SandboxError
from security import check_description, check_publication_eligibility, check_title
from ui.common import eligibility_tags, get_services

CATEGORIES = ["people", "event", "interior", "restaurant"]
CONSENTS = ["pending", "granted", "denied"]


def _setup_help() -> None:
    st.info("Google Photos is not set up on this PC yet.")
    st.markdown(
        "1. In the [Google Cloud console](https://console.cloud.google.com/), create a project and enable the "
        "**Google Photos Picker API**.\n"
        "2. In **Google Auth Platform**, keep the app in *Testing* and add your own address as a test user.\n"
        "3. Create an OAuth client of type **Desktop app** and download its JSON file.\n"
        "4. Save that file as:")
    st.code(str(gp.client_secret_path()), language=None)


def _choose(creds) -> None:
    client = gp.PickerClient(creds)
    session = st.session_state.get("gp_session")
    if session is None:
        if st.button("Choose photos", type="primary", icon=":material/add_photo_alternate:"):
            st.session_state["gp_session"] = client.create_session()
            st.rerun()
        return
    st.write(f"Choose up to {gp.MAX_ITEMS} photos on Google's page, press Done there, then come back here.")
    c1, c2, c3 = st.columns(3)
    c1.link_button("Open Google Photos", session["picker_uri"], icon=":material/open_in_new:", width="stretch")
    if c2.button("Import my selection", type="primary", width="stretch"):
        if not client.is_done(session["id"]):
            st.warning("Google has not received your selection yet. Press Done on Google's page first.")
        else:
            with st.spinner("Copying your photos into the sandbox"):
                result = import_session(client, get_services().sandbox, session["id"])
            st.session_state.pop("gp_session", None)
            st.success(f"{len(result['added'])} added, {len(result['existing'])} already there, "
                       f"{len(result['failed'])} skipped.")
            for message in sorted(set(result["failed"])):
                st.caption(message)
    if c3.button("Start over", width="stretch"):
        client.delete_session(session["id"])
        st.session_state.pop("gp_session", None)
        st.rerun()


def _draft_description(pid: str) -> None:
    """Button callback: the draft only fills the text box. Nothing is stored until Save."""
    try:
        st.session_state[f"desc_{pid}"] = describe(get_services().sandbox.image_path(pid))
        st.session_state[f"desc_msg_{pid}"] = None
    except (VisionError, SandboxError) as exc:
        st.session_state[f"desc_msg_{pid}"] = exc.message


def _rights_form(photo: dict) -> None:
    pid = photo["photo_id"]
    sb = get_services().sandbox
    left, right = st.columns([1, 2])
    left.image(str(sb.image_path(pid)), width="stretch")
    left.button("Describe this photo", key=f"look_{pid}", on_click=_draft_description, args=(pid,),
                icon=":material/visibility:", width="stretch",
                help="A model on this PC looks at the photo and drafts the description. Nothing is uploaded.")
    if st.session_state.get(f"desc_msg_{pid}"):
        left.error(st.session_state[f"desc_msg_{pid}"])
    st.session_state.setdefault(f"desc_{pid}", photo.get("description", ""))
    with right, st.form(f"rights_{pid}"):
        st.markdown(f'<span class="lp-muted">{html.escape(pid)}</span> {eligibility_tags(photo)}',
                    unsafe_allow_html=True)
        if check_publication_eligibility(photo).allowed:
            st.success("The agent can use this photo.")
        else:
            missing = (["turn on “May be shown publicly”"] if photo["visibility"] != "public" else []) + \
                      (["set consent to “granted”"] if photo["client_consent"] != "granted" else [])
            st.warning("The agent cannot use this photo yet. Still needed: " + " and ".join(missing) + ".")
        title = st.text_input("Title", value=photo["title"], max_chars=80)
        description = st.text_area("Description", key=f"desc_{pid}", max_chars=600,
                                   help="The agent writes captions from the title and this text. Check it, "
                                        "then press Save.")
        category = st.selectbox("Category", CATEGORIES, index=CATEGORIES.index(photo["category"]))
        public = st.toggle("May be shown publicly", value=photo["visibility"] == "public")
        consent = st.selectbox("Consent of the people in the photo", CONSENTS, index=CONSENTS.index(photo["client_consent"]))
        sure = st.checkbox("I confirm this is correct and that I have the right to publish this photo.")
        if st.form_submit_button("Save"):
            problems = check_title(title) + check_description(description)
            if (public or consent == "granted") and not sure:
                problems.append("tick the confirmation before opening a photo to publication")
            if problems:
                st.error("Not saved: " + "; ".join(problems) + ".")
                return
            sb.update_imported_photo(pid, title=title.strip(), category=category,
                                     visibility="public" if public else "private", client_consent=consent,
                                     description=description.strip())
            st.rerun()


def render() -> None:
    st.title("Import photos")
    st.markdown('<p class="lp-lede">Choose photos from your own Google Photos. They are copied into the local '
                "sandbox, without location data, and stay private until you mark them as publishable.</p>",
                unsafe_allow_html=True)
    try:
        if not gp.client_secret_path().is_file():
            _setup_help()
        else:
            creds = gp.load_credentials()
            if creds is None:
                st.write("Sign in with Google. A browser window opens on this PC.")
                if st.button("Sign in with Google", type="primary"):
                    with st.spinner("Waiting for Google (3 minutes at most)"):
                        gp.sign_in()
                    st.rerun()
            else:
                _choose(creds)
                if st.button("Disconnect Google", icon=":material/logout:"):
                    gp.sign_out()
                    st.session_state.pop("gp_session", None)
                    st.rerun()
    except (gp.GooglePhotosError, SandboxError) as exc:
        st.error(exc.message)

    imported = [p for p in get_services().sandbox.catalog() if p["photo_id"].startswith(ID_PREFIX)]
    st.subheader("Imported photos")
    if not imported:
        st.caption("Nothing imported yet.")
    else:
        usable = sum(check_publication_eligibility(p).allowed for p in imported)
        st.write(f"The agent can use **{usable} of {len(imported)}** photos. A photo needs **both** public "
                 "visibility and granted consent.")
    for photo in imported:
        _rights_form(photo)
