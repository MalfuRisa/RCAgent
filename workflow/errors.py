"""Error classification shared by node and task execution boundaries."""

from workspace.model_requests import ModelRequestError


def execution_error(error, role):
    if isinstance(error, ModelRequestError):
        return error.detail
    code = "investigation_deadline" if isinstance(error, TimeoutError) else role + "_incomplete"
    return {"code": code, "message": str(error)[:2000]}
