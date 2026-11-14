"""Application assembly. Run with ``uvicorn harness.app:app``."""

import os
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from harness.core.gateway import Gateway
from harness.interfaces.api import create_router

STATIC = Path(__file__).parent / 'interfaces' / 'static'
VERSION = '0.10.0'


def create_app(get_engine, *, with_ui=True):
    """Assemble the API and, optionally, the browser workspaces around a gateway provider."""
    application = FastAPI(title='OmniEdu Teaching Harness', version=VERSION)
    application.include_router(create_router(get_engine))
    if with_ui:
        # The pages hold no data; every data request goes through the authorized API.
        pages = {'/': 'index.html', '/student/': 'student.html', '/teacher/': 'teacher.html'}
        for route, filename in pages.items():
            application.add_api_route(route, _page(filename), include_in_schema=False)
        for bare in ('/student', '/teacher'):
            application.add_api_route(bare, _redirect(bare + '/'), include_in_schema=False)
        application.mount('/static', StaticFiles(directory=STATIC), name='static')
    return application


def _page(filename):
    def page():
        return FileResponse(STATIC / filename, headers={'Cache-Control': 'no-cache'})

    return page


def _redirect(target):
    def redirect():
        return RedirectResponse(target)

    return redirect


engine = Gateway(db=os.getenv('HARNESS_DB'))
app = create_app(lambda: engine)
