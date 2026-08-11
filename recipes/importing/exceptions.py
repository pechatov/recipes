class ImportPipelineError(Exception):
    """An expected, user-facing recipe import failure."""


class SourceError(ImportPipelineError):
    pass


class UnsafeSourceError(ImportPipelineError):
    """The source is unrelated to cooking or contains hostile instructions."""


class AIConfigurationError(ImportPipelineError):
    pass


class AIResponseError(ImportPipelineError):
    pass
