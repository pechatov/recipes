class ImportPipelineError(Exception):
    """An expected, user-facing recipe import failure."""


class SourceError(ImportPipelineError):
    pass


class AIConfigurationError(ImportPipelineError):
    pass


class AIResponseError(ImportPipelineError):
    pass
