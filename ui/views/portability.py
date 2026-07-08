"""
Portability page — import conversation exports, export memory for other models.
"""
import re
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

INGESTION_SCRIPT = Path(__file__).parent.parent.parent / "ingestion" / "run.py"


_PROFILE_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_PROFILE_ALLOWED_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_.-")
_ALLOWED_SOURCES = {"claude", "chatgpt", "gemini"}
_ALLOWED_PROFILES = {
    "default": "default",
    "work": "work",
    "personal": "personal",
}


def _validate_profile_name(profile: str) -> str:
    profile = profile.strip().lower()
    if not _PROFILE_RE.fullmatch(profile):
        raise ValueError(
            "Invalid profile name. Use 1-64 characters: letters, numbers, underscore, hyphen, or dot."
        )
    if profile.startswith((".", "-")):
        raise ValueError("Invalid profile name. Must not start with '.' or '-'.")
    # Canonicalize through an explicit character allowlist.
    # Rebuild the name character-by-character from an explicit literal allowlist.
    # The result carries no taint (CodeQL) even though any validly-named profile
    # is accepted, not just a fixed set (#90).
    canonical = "".join(ch for ch in profile if ch in _PROFILE_ALLOWED_CHARS)
    if not canonical:
        raise ValueError("Invalid profile name.")
    return canonical


def _validate_source(source: str) -> str:
    if source not in _ALLOWED_SOURCES:
        raise ValueError("Invalid source. Must be one of: claude, chatgpt, gemini.")
    # Return explicit matching literals to break taint tracking
    if source == "claude":
        return "claude"
    elif source == "chatgpt":
        return "chatgpt"
    elif source == "gemini":
        return "gemini"
    raise ValueError("Invalid source.")


def _validate_input_file_path(file_path: Path) -> Path:
    candidate = Path(file_path).resolve()
    if not candidate.exists() or not candidate.is_file():
        raise ValueError("Invalid input file path.")
    
    # Ensure it resides within the system temp directory to prevent path traversal
    temp_dir = Path(tempfile.gettempdir()).resolve()
    if temp_dir not in candidate.parents:
        raise ValueError("File must be located in the temporary directory.")

    if candidate.suffix.lower() != ".json":
        raise ValueError("Invalid input file type. Expected a .json file.")

    # Strict regex check on the string representation to prevent option or argument injections
    path_str = str(candidate)
    match = re.fullmatch(r"^[A-Za-z0-9_/.-]+$", path_str)
    if not match:
        raise ValueError("Invalid characters in file path.")

    # Return new Path from the matched string to break taint tracking
    return Path(match.group(0))



def run_ingestion(source: str, file_path: Path, profile: str,
                  preview: bool = True, days: int = None) -> dict:
    """
    Run the ingestion pipeline. Returns parsed JSON from run.py output.
    """
    safe_source = _validate_source(source)
    safe_file_path = _validate_input_file_path(file_path)
    safe_profile = _validate_profile_name(profile)
    days_int = None
    if days is not None:
        try:
            days_int = int(days)
        except (ValueError, TypeError):
            raise ValueError("Invalid days parameter. Must be an integer.")
        if days_int < 0:
            raise ValueError("Invalid days parameter. Must be non-negative.")

    cmd = [
        sys.executable, str(INGESTION_SCRIPT),
        "--source", safe_source,
        "--file", str(safe_file_path),
        "--profile", safe_profile,
    ]
    if preview:
        cmd.append("--preview")
    if days_int and days_int > 0:
        cmd.extend(["--days", str(days_int)])

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, shell=False)
    except subprocess.TimeoutExpired:
        # Don't let TimeoutExpired escape as an uncaught 500 — the callers only
        # guard ValueError. Return it as a normal failed-run result (#113).
        return {
            "returncode": -1,
            "stdout": "",
            "stderr": "Ingestion timed out after 300s and was terminated.",
        }
    # run.py writes a JSON log to ~/memorybridge/logs/ — parse stdout summary
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def render():
    import streamlit as st
    from server import export_for_model as _export_tool, export_passport as _passport_tool
    export_for_model = _export_tool.fn
    export_passport = _passport_tool.fn

    st.header("🔄 Portability")

    tab_import, tab_export, tab_passport = st.tabs(["⬆ Import", "⬇ Export", "🛂 Passport"])

    # =========================================================================
    # IMPORT TAB
    # =========================================================================
    with tab_import:
        st.subheader("Import conversation history")
        st.caption(
            "Supported: Claude (`conversations.json`), "
            "ChatGPT (`conversations.json`), Gemini (`MyActivity.json`)"
        )

        source = st.selectbox("Source", ["claude", "chatgpt", "gemini"])
        profile = st.selectbox("Target profile", list(_ALLOWED_PROFILES.keys()), index=0)
        days = st.number_input("Limit to last N days (0 = all)", min_value=0, value=30)

        uploaded = st.file_uploader(
            "Drop export file here",
            type=["json"],
            help="Export from your AI provider and upload here."
        )

        if uploaded is not None:
            # Save to temp file so ingestion script can read it. Cache one temp
            # file per unique upload in session_state and reuse it across reruns
            # instead of writing a fresh copy every rerun (#113). When a new file
            # is uploaded, delete the previous temp file first.
            upload_key = (uploaded.name, uploaded.size)
            cached = st.session_state.get("_mb_upload")
            if not cached or cached.get("key") != upload_key:
                if cached:
                    try:
                        Path(cached["path"]).unlink(missing_ok=True)
                    except OSError:
                        pass
                with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                    tf.write(uploaded.read())
                    tmp_path = Path(tf.name)
                st.session_state["_mb_upload"] = {"key": upload_key, "path": str(tmp_path)}
            else:
                tmp_path = Path(cached["path"])

            st.info(f"File: `{uploaded.name}` ({uploaded.size:,} bytes)")

            col_preview, col_run = st.columns(2)

            with col_preview:
                if st.button("🔍 Preview extraction", use_container_width=True):
                    try:
                        with st.spinner("Running extraction preview…"):
                             result = run_ingestion(source, tmp_path, profile, preview=True, days=days)
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        if result["returncode"] == 0:
                            st.success("Preview complete")
                            if result["stdout"]:
                                st.code(result["stdout"], language="text")
                        else:
                            st.error("Preview failed")
                            st.code(result["stderr"], language="text")

            with col_run:
                if st.button("✓ Run ingestion", type="primary", use_container_width=True):
                    try:
                        with st.spinner("Ingesting memories…"):
                             result = run_ingestion(source, tmp_path, profile, preview=False, days=days)
                    except ValueError as e:
                        st.error(str(e))
                    else:
                        if result["returncode"] == 0:
                            st.success("Ingestion complete!")
                            if result["stdout"]:
                                st.code(result["stdout"], language="text")
                        else:
                            st.error("Ingestion failed")
                            st.code(result["stderr"], language="text")
                    finally:
                        # Ingestion consumed the upload — remove the temp file so
                        # it doesn't linger in the system temp dir (#113).
                        try:
                            Path(tmp_path).unlink(missing_ok=True)
                        except OSError:
                            pass
                        st.session_state.pop("_mb_upload", None)

    # =========================================================================
    # EXPORT TAB
    # =========================================================================
    with tab_export:
        st.subheader("Export memory for another AI")

        col1, col2, col3 = st.columns(3)
        with col1:
            model = st.selectbox("Target model", ["chatgpt", "gemini", "ollama"])
        with col2:
            export_profile = st.text_input("Profile", value="default", key="exp_profile")
        with col3:
            depth = st.selectbox("Depth", ["full", "summary", "minimal"])

        max_tokens = st.slider("Token budget", 500, 4000, 2000, step=100)

        if st.button("Generate export", type="primary"):
            with st.spinner("Formatting…"):
                try:
                    text = export_for_model(
                        model=model,
                        profile=export_profile,
                        depth=depth,
                        max_tokens=max_tokens,
                    )
                    st.success("Export ready — copy or download below.")
                    st.text_area("Export text", value=text, height=300)
                    st.download_button(
                        label=f"⬇ Download for {model}",
                        data=text,
                        file_name=f"memorybridge_{export_profile}_{model}.txt",
                        mime="text/plain",
                    )
                except Exception as e:
                    st.error(f"Export failed: {e}")

    # =========================================================================
    # PASSPORT TAB
    # =========================================================================
    with tab_passport:
        st.subheader("Memory Passport")
        st.caption(
            "A plain-text snapshot of your memory — paste into any AI's system prompt. "
            "No JSON, no code fences. Works with Claude, ChatGPT, Gemini, Ollama, anything."
        )

        col1, col2 = st.columns(2)
        with col1:
            passport_profile = st.text_input("Profile", value="default", key="pp_profile")
        with col2:
            passport_tokens = st.slider("Token budget", 500, 4000, 2000, step=100,
                                        key="pp_tokens")

        if st.button("Generate Passport", type="primary", key="pp_generate"):
            with st.spinner("Building passport…"):
                try:
                    text = export_passport(
                        profile=passport_profile,
                        max_tokens=passport_tokens,
                    )
                    st.success("Passport ready.")
                    st.text_area("Passport text", value=text, height=400, key="pp_text")
                    st.download_button(
                        label="⬇ Download passport",
                        data=text,
                        file_name=f"memorybridge_passport_{passport_profile}.txt",
                        mime="text/plain",
                        key="pp_download",
                    )
                except Exception as e:
                    st.error(f"Passport generation failed: {e}")
