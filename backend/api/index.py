"""Serverless entrypoint.

Vercel's Python runtime imports this file and serves whatever `app` is. It is the
same application `uvicorn app.main:app` serves, so there is exactly one codebase and
no second version to keep in step.
"""
from app.main import app

__all__ = ["app"]
