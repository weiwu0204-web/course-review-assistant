from typing import Annotated
from pydantic import StringConstraints

Id = Annotated[str, StringConstraints(pattern=r"^[a-f0-9]{32}$")]
