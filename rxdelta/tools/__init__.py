"""Typed, read-only tools over a loaded rxdelta database.

Each tool takes a Pydantic input model and returns a ToolResult. None of them
writes to the database: every connection is opened with mode=ro.
"""
