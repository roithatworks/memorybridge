"""
Portability page — import conversation exports, export memory for other models.
"""
import json
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

INGESTION_SCRIPT = Path(__file__).parent.parent.parent / "ingestion" / "run.py"


def run_ingestion(source: str, file_path: Path, profile: str,
                  preview: bool = True) -> dict:
    """
    Run the ingestion pipeline. Returns parsed JSON from run.py output.
    """
    cmd = [
        sys.executable, str(INGESTION_SCRIPT),
        "--source", source,
        "--file", str(file_path),
        "--profile", profile,
    ]
    if preview:
        cmd.append("--preview")

    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    # run.py writes a JSON log to ~/memorybridge/logs/ — parse stdout summary
    return {
        "returncode": result.returncode,
        "stdout": result.stdout,
        "stderr": result.stderr,
    }


def render():
    import streamlit as st
    from server import export_for_model as _export_tool
    export_for_model = _export_tool.fn

    st.header("🔄 Portability")

    tab_import, tab_export = st.tabs(["⬆ Import", "⬇ Export"])

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
        profile = st.text_input("Target profile", value="default")
        days = st.number_input("Limit to last N days (0 = all)", min_value=0, value=30)

        uploaded = st.file_uploader(
            "Drop export file here",
            type=["json"],
            help="Export from your AI provider and upload here."
        )

        if uploaded is not None:
            # Save to temp file so ingestion script can read it
            with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tf:
                tf.write(uploaded.read())
                tmp_path = Path(tf.name)

            st.info(f"File: `{uploaded.name}` ({uploaded.size:,} bytes)")

            col_preview, col_run = st.columns(2)

            with col_preview:
                if st.button("🔍 Preview extraction", use_container_width=True):
                    with st.spinner("Running extraction preview…"):
                        result = run_ingestion(source, tmp_path, profile, preview=True)
                    if result["returncode"] == 0:
                        st.success("Preview complete")
                        if result["stdout"]:
                            st.code(result["stdout"], language="text")
                    else:
                        st.error("Preview failed")
                        st.code(result["stderr"], language="text")

            with col_run:
                if st.button("✓ Run ingestion", type="primary", use_container_width=True):
                    with st.spinner("Ingesting memories…"):
                        result = run_ingestion(source, tmp_path, profile, preview=False)
                    if result["returncode"] == 0:
                        st.success("Ingestion complete!")
                        if result["stdout"]:
                            st.code(result["stdout"], language="text")
                    else:
                        st.error("Ingestion failed")
                        st.code(result["stderr"], language="text")

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
