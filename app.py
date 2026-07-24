import os
import re
from typing import Optional

import boto3
import gradio as gr
from botocore.config import Config
from botocore.exceptions import ClientError


# ============================================================
# CONFIGURATION
# ============================================================

# Example:
# S3_ENDPOINT=https://s3.us-west-1.idrivee2.com
# S3_REGION=us-west-1
# S3_ACCESS_KEY=xxxxx
# S3_SECRET_KEY=xxxxx
# S3_BUCKET=my-bucket
# MD_ROOT_PREFIX=spark-memory

S3_ENDPOINT = os.getenv("S3_ENDPOINT")
S3_REGION = os.getenv("S3_REGION", "us-west-1")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET = os.getenv("S3_BUCKET")

# Everything the agent can access must live below this prefix.
MD_ROOT_PREFIX = os.getenv(
    "MD_ROOT_PREFIX",
    "spark-memory"
).strip("/")


# ============================================================
# VALIDATE CONFIGURATION
# ============================================================

required_config = {
    "S3_ENDPOINT": S3_ENDPOINT,
    "S3_ACCESS_KEY": S3_ACCESS_KEY,
    "S3_SECRET_KEY": S3_SECRET_KEY,
    "S3_BUCKET": S3_BUCKET,
}

missing = [
    key
    for key, value in required_config.items()
    if not value
]

if missing:
    raise RuntimeError(
        "Missing required environment variables: "
        + ", ".join(missing)
    )


# ============================================================
# S3 CLIENT
# ============================================================

s3 = boto3.client(
    "s3",
    endpoint_url=S3_ENDPOINT,
    region_name=S3_REGION,
    aws_access_key_id=S3_ACCESS_KEY,
    aws_secret_access_key=S3_SECRET_KEY,
    config=Config(
        signature_version="s3v4",
        retries={
            "max_attempts": 3,
            "mode": "standard"
        }
    ),
)


# ============================================================
# PATH SECURITY
# ============================================================

def normalize_file_path(file_path: str) -> str:
    """
    Validate a Markdown file path and convert it into an S3 key.
    The agent is restricted to:
        MD_ROOT_PREFIX/**.md
    Example:
        input:
            project-123/context.md
        output:
            spark-memory/project-123/context.md
    """

    if not file_path:
        raise ValueError(
            "file_path is required."
        )

    file_path = (
        file_path
        .strip()
        .replace("\\", "/")
        .lstrip("/")
    )

    if not file_path:
        raise ValueError(
            "file_path cannot be empty."
        )

    parts = file_path.split("/")

    if ".." in parts:
        raise ValueError(
            "Path traversal is not allowed."
        )

    if "." in parts:
        raise ValueError(
            "Relative paths are not allowed."
        )

    if not file_path.lower().endswith(".md"):
        raise ValueError(
            "Only Markdown (.md) files are allowed."
        )

    return f"{MD_ROOT_PREFIX}/{file_path}"


def normalize_prefix(prefix: Optional[str]) -> str:
    """
    Validate a directory/prefix for list operations.
    """

    if not prefix:
        return MD_ROOT_PREFIX + "/"

    prefix = (
        prefix
        .strip()
        .replace("\\", "/")
        .strip("/")
    )

    if ".." in prefix.split("/"):
        raise ValueError(
            "Invalid prefix."
        )

    return f"{MD_ROOT_PREFIX}/{prefix}/"


def display_path(s3_key: str) -> str:
    """
    Remove internal root prefix from an S3 key.
    """

    root = MD_ROOT_PREFIX + "/"

    if s3_key.startswith(root):
        return s3_key[len(root):]

    return s3_key


# ============================================================
# INTERNAL S3 FUNCTIONS
# ============================================================

def _read_s3_object(key: str) -> str:
    """
    Internal S3 object reader.
    """

    try:
        response = s3.get_object(
            Bucket=S3_BUCKET,
            Key=key
        )

        return (
            response["Body"]
            .read()
            .decode("utf-8")
        )

    except ClientError as exc:

        code = (
            exc.response
            .get("Error", {})
            .get("Code", "Unknown")
        )

        if code in {
            "NoSuchKey",
            "404",
            "NotFound"
        }:
            raise FileNotFoundError(
                f"Markdown file not found: "
                f"{display_path(key)}"
            )

        raise RuntimeError(
            f"S3 read failed: {code}"
        )


def _write_s3_object(
    key: str,
    content: str
) -> None:
    """
    Internal S3 object writer.
    """

    try:
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=key,
            Body=content.encode("utf-8"),
            ContentType="text/markdown; charset=utf-8"
        )

    except ClientError as exc:

        code = (
            exc.response
            .get("Error", {})
            .get("Code", "Unknown")
        )

        raise RuntimeError(
            f"S3 write failed: {code}"
        )


# ============================================================
# MCP TOOL 1
# LIST MARKDOWN FILES
# ============================================================

def list_md_files(prefix: str = "") -> str:
    """
    List Markdown files available in persistent S3 storage.
    Use this tool when you need to discover which Markdown
    files exist before deciding which file to read or modify.
    Args:
        prefix:
            Optional directory prefix.
            Example:
                projects/project-123
            Leave empty to list all accessible Markdown files.
    Returns:
        Newline-separated relative Markdown file paths.
    """

    try:
        s3_prefix = normalize_prefix(prefix)

        paginator = s3.get_paginator(
            "list_objects_v2"
        )

        pages = paginator.paginate(
            Bucket=S3_BUCKET,
            Prefix=s3_prefix
        )

        files = []

        for page in pages:

            for obj in page.get(
                "Contents",
                []
            ):

                key = obj["Key"]

                if key.lower().endswith(".md"):

                    files.append(
                        display_path(key)
                    )

        files.sort()

        if not files:
            return "No Markdown files found."

        return "\n".join(files)

    except Exception as exc:
        return f"ERROR: {str(exc)}"


# ============================================================
# MCP TOOL 2
# READ MARKDOWN FILE
# ============================================================

def read_md_file(file_path: str) -> str:
    """
    Read the complete content of a Markdown file from
    persistent S3 storage.
    Use this tool when you need existing project context,
    state, architecture, decisions, checkpoints, notes,
    documentation, or other Markdown-based memory.
    Args:
        file_path:
            Relative Markdown file path.
            Example:
                projects/project-123/context.md
    Returns:
        Complete Markdown content.
    """

    try:
        key = normalize_file_path(
            file_path
        )

        return _read_s3_object(
            key
        )

    except Exception as exc:
        return f"ERROR: {str(exc)}"


# ============================================================
# MCP TOOL 3
# WRITE MARKDOWN FILE
# ============================================================

def write_md_file(
    file_path: str,
    content: str
) -> str:
    """
    Create or completely overwrite a Markdown file.
    WARNING:
        This operation replaces the entire existing file.
    Prefer update_md_section when only one section needs
    to change.
    Args:
        file_path:
            Relative Markdown path.
            Example:
                projects/project-123/context.md
        content:
            Complete Markdown content to save.
    Returns:
        Operation status.
    """

    try:
        key = normalize_file_path(
            file_path
        )

        if content is None:
            content = ""

        _write_s3_object(
            key,
            content
        )

        return (
            "SUCCESS\n"
            f"Operation: write\n"
            f"File: {file_path}"
        )

    except Exception as exc:
        return f"ERROR: {str(exc)}"


# ============================================================
# MCP TOOL 4
# APPEND MARKDOWN
# ============================================================

def append_md_file(
    file_path: str,
    content: str
) -> str:
    """
    Append new Markdown content to the end of a file.
    Use this tool for:
        - execution history
        - logs
        - new decisions
        - chronological notes
        - checkpoints
        - audit history
    If the file does not exist, it will be created.
    Args:
        file_path:
            Relative Markdown path.
        content:
            Markdown content to append.
    Returns:
        Operation status.
    """

    try:
        key = normalize_file_path(
            file_path
        )

        if not content:
            raise ValueError(
                "content is required."
            )

        try:
            existing = _read_s3_object(
                key
            )

        except FileNotFoundError:
            existing = ""

        if existing:
            updated_content = (
                existing.rstrip()
                + "\n\n"
                + content.strip()
                + "\n"
            )

        else:
            updated_content = (
                content.strip()
                + "\n"
            )

        _write_s3_object(
            key,
            updated_content
        )

        return (
            "SUCCESS\n"
            f"Operation: append\n"
            f"File: {file_path}"
        )

    except Exception as exc:
        return f"ERROR: {str(exc)}"


# ============================================================
# MCP TOOL 5
# UPDATE MARKDOWN SECTION
# ============================================================

def update_md_section(
    file_path: str,
    heading: str,
    content: str
) -> str:
    """
    Update a specific Markdown section without replacing
    the entire document.
    If the requested heading does not exist, a new H2
    section will be appended.
    Example existing document:
        # Project
        ## Architecture
        Old architecture.
        ## Database
        PostgreSQL.
    Calling:
        heading = "Architecture"
    replaces only the Architecture section.
    Args:
        file_path:
            Relative Markdown path.
        heading:
            Markdown heading name without # characters.
            Example:
                Architecture
        content:
            New section content.
    Returns:
        Operation status.
    """

    try:
        key = normalize_file_path(
            file_path
        )

        markdown = _read_s3_object(
            key
        )

        if not heading:
            raise ValueError(
                "heading is required."
            )

        heading = (
            heading
            .strip()
            .lstrip("#")
            .strip()
        )

        if not heading:
            raise ValueError(
                "Invalid heading."
            )

        if content is None:
            content = ""

        # ----------------------------------------------------
        # Find heading and its content.
        #
        # Stops at a heading of the same or higher level.
        # ----------------------------------------------------

        heading_pattern = re.compile(
            rf"(?m)^(?P<hashes>#{{1,6}})"
            rf"[ \t]+{re.escape(heading)}"
            rf"[ \t]*$"
        )

        match = heading_pattern.search(
            markdown
        )

        if match:

            hashes = match.group(
                "hashes"
            )

            level = len(hashes)

            start = match.start()

            next_heading_pattern = re.compile(
                rf"(?m)^#{{1,{level}}}[ \t]+.+$"
            )

            next_match = next_heading_pattern.search(
                markdown,
                match.end()
            )

            if next_match:
                end = next_match.start()
            else:
                end = len(markdown)

            replacement = (
                f"{hashes} {heading}\n\n"
                f"{content.strip()}\n\n"
            )

            markdown = (
                markdown[:start]
                + replacement
                + markdown[end:]
            )

            operation = "section_updated"

        else:

            markdown = (
                markdown.rstrip()
                + "\n\n"
                + f"## {heading}\n\n"
                + content.strip()
                + "\n"
            )

            operation = "section_created"

        _write_s3_object(
            key,
            markdown
        )

        return (
            "SUCCESS\n"
            f"Operation: {operation}\n"
            f"File: {file_path}\n"
            f"Heading: {heading}"
        )

    except Exception as exc:
        return f"ERROR: {str(exc)}"


# ============================================================
# GRADIO APPLICATION
# ============================================================

with gr.Blocks(
    title="S3 Markdown MCP Server"
) as demo:

    gr.Markdown(
        """
# S3 Markdown Memory
Gradio + MCP interface for Markdown files stored in
S3-compatible object storage.
### MCP Tools
- `list_md_files`
- `read_md_file`
- `write_md_file`
- `append_md_file`
- `update_md_section`
"""
    )

    # ========================================================
    # LIST FILES
    # ========================================================

    with gr.Tab("List Files"):

        list_prefix = gr.Textbox(
            label="Prefix",
            placeholder="projects/project-123"
        )

        list_button = gr.Button(
            "List Markdown Files",
            variant="primary"
        )

        list_result = gr.Textbox(
            label="Files",
            lines=15
        )

        list_button.click(
            fn=list_md_files,
            inputs=[
                list_prefix
            ],
            outputs=[
                list_result
            ],
            api_name="list_md_files"
        )

    # ========================================================
    # READ
    # ========================================================

    with gr.Tab("Read"):

        read_path = gr.Textbox(
            label="File Path",
            placeholder=(
                "projects/project-123/"
                "context.md"
            )
        )

        read_button = gr.Button(
            "Read Markdown",
            variant="primary"
        )

        read_result = gr.Textbox(
            label="Markdown Content",
            lines=25
        )

        read_button.click(
            fn=read_md_file,
            inputs=[
                read_path
            ],
            outputs=[
                read_result
            ],
            api_name="read_md_file"
        )

    # ========================================================
    # WRITE
    # ========================================================

    with gr.Tab("Write"):

        write_path = gr.Textbox(
            label="File Path",
            placeholder=(
                "projects/project-123/"
                "context.md"
            )
        )

        write_content = gr.Textbox(
            label="Markdown Content",
            placeholder=(
                "# Project Context\n\n"
                "Project information..."
            ),
            lines=20
        )

        write_button = gr.Button(
            "Write Markdown",
            variant="primary"
        )

        write_result = gr.Textbox(
            label="Result"
        )

        write_button.click(
            fn=write_md_file,
            inputs=[
                write_path,
                write_content
            ],
            outputs=[
                write_result
            ],
            api_name="write_md_file"
        )

    # ========================================================
    # APPEND
    # ========================================================

    with gr.Tab("Append"):

        append_path = gr.Textbox(
            label="File Path",
            placeholder=(
                "projects/project-123/"
                "decisions.md"
            )
        )

        append_content = gr.Textbox(
            label="Content to Append",
            placeholder=(
                "## Decision\n\n"
                "Decision details..."
            ),
            lines=15
        )

        append_button = gr.Button(
            "Append Markdown",
            variant="primary"
        )

        append_result = gr.Textbox(
            label="Result"
        )

        append_button.click(
            fn=append_md_file,
            inputs=[
                append_path,
                append_content
            ],
            outputs=[
                append_result
            ],
            api_name="append_md_file"
        )

    # ========================================================
    # UPDATE SECTION
    # ========================================================

    with gr.Tab("Update Section"):

        update_path = gr.Textbox(
            label="File Path",
            placeholder=(
                "projects/project-123/"
                "architecture.md"
            )
        )

        update_heading = gr.Textbox(
            label="Section Heading",
            placeholder="Database Architecture"
        )

        update_content = gr.Textbox(
            label="New Section Content",
            placeholder=(
                "PostgreSQL is used as the "
                "primary database..."
            ),
            lines=15
        )

        update_button = gr.Button(
            "Update Section",
            variant="primary"
        )

        update_result = gr.Textbox(
            label="Result"
        )

        update_button.click(
            fn=update_md_section,
            inputs=[
                update_path,
                update_heading,
                update_content
            ],
            outputs=[
                update_result
            ],
            api_name="update_md_section"
        )


# ============================================================
# START GRADIO + MCP SERVER
# ============================================================

if __name__ == "__main__":

    demo.launch(
        server_name="0.0.0.0",
        server_port=7860,
        mcp_server=True
    )
