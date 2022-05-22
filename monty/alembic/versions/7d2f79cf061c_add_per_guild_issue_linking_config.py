"""add per-guild issue linking config

Revision ID: 7d2f79cf061c
Revises: 50ddfc74e23c
Create Date: 2022-05-22 04:25:19.100644

"""
import sqlalchemy as sa
from alembic import op


# revision identifiers, used by Alembic.
revision = "7d2f79cf061c"
down_revision = "50ddfc74e23c"
branch_labels = None
depends_on = None


def upgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.add_column("guild_config", sa.Column("github_issues_org", sa.String(length=39), nullable=True))
    # ### end Alembic commands ###


def downgrade():
    # ### commands auto generated by Alembic - please adjust! ###
    op.drop_column("guild_config", "github_issues_org")
    # ### end Alembic commands ###
