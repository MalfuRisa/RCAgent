"""Small local material/skill interface; no workspace gate or arbitrary filesystem."""
from workspace.skill_knowledge import load, directory


def definition(name, description, properties, required=()):
    return {"type": "function", "function": {"name": name, "description": description,
        "parameters": {"type": "object", "properties": properties, "required": list(required)}}}


def local_tools(outputs):
    text = {"type": "string"}
    integer = {"type": "integer", "minimum": 0}
    definitions = [
        definition("read_material", "Read this run's full original source by JSON Pointer; paginate arrays or strings.", {
            "source_id": text, "pointer": {**text, "default": ""},
            "offset": {**integer, "default": 0}, "limit": {**integer, "minimum": 1, "default": 100},
            "max_chars": {**integer, "minimum": 1, "default": 16000}}, ["source_id"]),
        definition("search_materials", "Search only mounted local materials. History is never current evidence.", {
            "query": text, "scope": {"type": "string", "enum": ["current", "history"], "default": "current"}}, ["query"]),
        definition("read_skill", "Read registered project guidance, not incident evidence; references are one level.", {
            "name": text, "resource": {**text, "default": "SKILL.md"}}, ["name"])]

    def read_skill(name, resource="SKILL.md"):
        import os
        if os.getenv('RCAGENT_MCP_PROFILE', 'native') == 'business' and name != 'kylin-insights-observability':
            raise ValueError('Business profile guidance: use kylin-insights-observability only')
        content, allowed = load(name, resource)
        return {"name": name, "resource": resource, "content": content, "available": allowed, "guidance_only": True}

    return {d["function"]["name"]: d for d in definitions}, {
        "read_material": outputs.materials.read, "search_materials": outputs.materials.search, "read_skill": read_skill}


def skill_directory():
    return directory()
