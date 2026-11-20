"""Native ChimeraX bundle entry points. GUI imports are intentionally lazy."""
from chimerax.core.toolshed import BundleAPI


class _IllustructAPI(BundleAPI):
    api_version = 1

    @staticmethod
    def start_tool(session, bundle_info, tool_info):
        from .tool import IllustructTool
        return IllustructTool.get_singleton(session)

    @staticmethod
    def register_command(bundle_info, command_info, logger):
        from .commands import register_commands
        register_commands(logger)

    @staticmethod
    def get_class(class_name):
        if class_name == "IllustructTool":
            from .tool import IllustructTool
            return IllustructTool
        return None


bundle_api = _IllustructAPI()
