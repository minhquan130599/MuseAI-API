 1 | class MuseError(RuntimeError):
 2 |     """Base error for the reverse-engineered Muse client."""
 3 | 
 4 | 
 5 | class MuseAuthError(MuseError):
 6 |     pass
 7 | 
 8 | 
 9 | class MuseProtocolError(MuseError):
10 |     pass
11 | 
12 | 
13 | class MuseRpcError(MuseError):
14 |     def __init__(self, message: str, *, status: int | None = None, body: bytes | None = None) -> None:
15 |         super().__init__(message)
16 |         self.status = status
17 |         self.body = body
18 | 