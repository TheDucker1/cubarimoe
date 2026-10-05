class DownloadError(Exception):
    """Only fixed, public-safe messages may be used with this exception."""


class Cancelled(DownloadError):
    pass


class Blocked(DownloadError):
    pass
