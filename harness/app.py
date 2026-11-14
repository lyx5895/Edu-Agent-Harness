"""Application assembly. Run with uvicorn harness.app:app."""
import os

import gradio as gr
from fastapi import FastAPI

from harness.interfaces.access import authorize
from harness.interfaces.api import create_router
from harness.core.gateway import Gateway
from harness.interfaces.web import create_ui


def create_app(get_engine, *, with_ui=True):
    """Assemble routes and optional UI around a supplied gateway provider."""
    application = FastAPI(title='OmniEdu Teaching Harness', version='0.9.0')

    @application.middleware('http')
    async def classroom_access(request, call_next):
        return await authorize(request, call_next, get_engine())

    router, endpoints = create_router(get_engine)
    application.include_router(router)
    application.state.endpoints = endpoints
    if with_ui:
        demo, callbacks = create_ui(get_engine, endpoints)
        application = gr.mount_gradio_app(application, demo, path='/', show_error=True)
        application.state.demo = demo
        application.state.callbacks = callbacks
    return application


engine = Gateway(db=os.getenv('HARNESS_DB'))
app = create_app(lambda: engine)
demo = app.state.demo

# Preserve the original callback entrypoints for scripts and integrations.
student_view = app.state.callbacks.student_view
start = app.state.callbacks.start
resume = app.state.callbacks.resume
operate = app.state.callbacks.operate
teacher = app.state.callbacks.teacher
revision = app.state.callbacks.revision
image_data = app.state.callbacks.image_data
start_task = app.state.callbacks.start_task
inbox_options = app.state.callbacks.inbox_options
start_inbox = app.state.callbacks.start_inbox
pack_display = app.state.callbacks.pack_display
generate_pack_ui = app.state.callbacks.generate_pack_ui
edited_pack_body = app.state.callbacks.edited_pack_body
save_pack_ui = app.state.callbacks.save_pack_ui
publish_pack_ui = app.state.callbacks.publish_pack_ui
suggest_knowledge_ui = app.state.callbacks.suggest_knowledge_ui
fill_path_ui = app.state.callbacks.fill_path_ui
next_text = app.state.callbacks.next_text
followups_text = app.state.callbacks.followups_text
dashboard_ui = app.state.callbacks.dashboard_ui
pick_review = app.state.callbacks.pick_review
preview_binding = app.state.callbacks.preview_binding
publish_ui = app.state.callbacks.publish_ui
publish_path_ui = app.state.callbacks.publish_path_ui
assign_ui = app.state.callbacks.assign_ui
revise_ui = app.state.callbacks.revise_ui
confirm_review_ui = app.state.callbacks.confirm_review_ui
revise_open = app.state.callbacks.revise_open
revision_ui = app.state.callbacks.revision_ui
show_image = app.state.callbacks.show_image
