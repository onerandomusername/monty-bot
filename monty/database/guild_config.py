from typing import Optional

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, MappedAsDataclass, mapped_column, relationship

from monty import constants

from .base import Base
from .guild import Guild


# n.b. make sure the metadata in config_metadata stays synced to this file and vice versa
class GuildConfig(MappedAsDataclass, Base):
    """Represents a per-guild config."""

    __tablename__ = "guild_config"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True, autoincrement=False)
    guild_id: Mapped[Optional[int]] = mapped_column(sa.ForeignKey("guilds.id"), name="guild", unique=True)
    guild: Mapped[Optional[Guild]] = relationship(Guild, default=None)
    prefix: Mapped[Optional[str]] = mapped_column(
        sa.String(length=50), nullable=True, default=constants.Client.default_command_prefix
    )
    github_issues_org: Mapped[Optional[str]] = mapped_column(sa.String(length=39), nullable=True, default=None)
    git_file_expansions: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    github_issue_linking: Mapped[bool] = mapped_column(sa.Boolean, default=True)
    github_comment_linking: Mapped[bool] = mapped_column(sa.Boolean, default=True)
