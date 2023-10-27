import uuid
import typing
import asyncio
import datetime
import sqlalchemy.ext.asyncio
from sqlalchemy.ext.asyncio import AsyncAttrs
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy import ForeignKey
from sqlalchemy.orm import Mapped
from sqlalchemy.orm import mapped_column
from sqlalchemy.orm import MappedAsDataclass
from sqlalchemy.orm import DeclarativeBase
from sqlalchemy.orm import relationship
from sqlalchemy import func
from sqlalchemy.sql import select
import pydantic


# ORM Models

class BaseModel(
    MappedAsDataclass,   # this basically says, that it will map to any dataclass (including native or pydantic)
    DeclarativeBase,     # needed for declaritive model definitions
    AsyncAttrs,          # proxies the relationship fields to model.awaitable_attrs.{your_relationship_scalar_or_vector}
    # dataclass_callable=pydantic.dataclasses.dataclass,
):
    pass    

    # The orm_mode = True setting didn't seem to affect anything in my incremental tests.
    #
    # class Config:
        # orm_mode = True

    # Another thing I learned is the DeclaritiveBase and orm_explicit_declarative_base(),
    # where the orm_explicit_declarative_base will only map fields that are
    # type hinted with Mapped[] type.


class IdentifiedModel:
    # I've changed the `server_default=` to runtime generated `insert_default=`, as sqlalchemy doesn't have the uuid generator in its dialect for sqlite
    id: Mapped[uuid.UUID] = mapped_column(init=False, primary_key=True, insert_default=uuid.uuid4)


class TimestampModel:
    # I've changed the sqlalchemy.func.statement_timestamp() to func.now() as sqlalchemy doesn't have that in its dialect for sqlite
    created_at: Mapped[datetime.datetime] = mapped_column(init=False, server_default=sqlalchemy.func.now())
    updated_at: Mapped[typing.Optional[datetime.datetime]] = mapped_column(init=False, server_default=sqlalchemy.func.now(), nullable=True)


class User(BaseModel, IdentifiedModel, TimestampModel):
    __tablename__ = "users"

    first_name: Mapped[str]
    last_name: Mapped[str]

    # Relationships
    organization_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("organizations.id"), init=False)
    organization: Mapped['Organization'] = relationship(back_populates="users")


class Organization(BaseModel, IdentifiedModel, TimestampModel):
    __tablename__ = "organizations"

    name: Mapped[str]

    # Fuck, sqlite doesn't have ARRAY columns, it has JSON, but not ARRAYs
    # dba_names: Mapped[typing.List[str]] = mapped_column(sqlalchemy.ARRAY(sqlalchemy.String))
    my_dict: Mapped[typing.Dict[str, str]] = mapped_column(sqlalchemy.JSON(), default_factory=lambda: {})

    # Relationsips
    users: Mapped[typing.List[User]] = relationship(back_populates="organization", init=False)


class UserPydantic(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(from_attributes=True)

    first_name: str
    last_name: str

    organization: 'OrganizationPydantic'


class OrganizationPydantic(pydantic.BaseModel):
    model_config = pydantic.ConfigDict(from_attributes=True)

    name: str

    my_dict: typing.Dict[str, str]
    users: typing.List[User]


# Setup and async_session

DATABASE_URL = "sqlite+aiosqlite:///./test.db"

async def create_engine_and_tables():
    engine = sqlalchemy.ext.asyncio.create_async_engine(DATABASE_URL, echo=True)

    async with engine.begin() as conn:
        await conn.run_sync(BaseModel.metadata.drop_all)

    async with engine.begin() as conn:
        await conn.run_sync(BaseModel.metadata.create_all)
        
    return engine


from contextlib import asynccontextmanager

@asynccontextmanager
async def get_async_session(engine) -> sqlalchemy.ext.asyncio.AsyncSession:
    session = sqlalchemy.orm.sessionmaker(
        engine,
        class_=sqlalchemy.ext.asyncio.AsyncSession,
        expire_on_commit=False
    )

    async with session() as s:
        yield s


# Main

async def async_main():
    import logging
    logging.basicConfig()
    logging.getLogger('sqlalchemy').setLevel(logging.INFO)

    engine = await create_engine_and_tables()

    # pydantic.dataclasses.rebuild_dataclass(cls=Organization)
    # pydantic.dataclasses.rebuild_dataclass(cls=User)
    
    # Let's create some records and write them to the db
    async with get_async_session(engine) as sesh:
        session: sqlalchemy.ext.asyncio.AsyncSession = sesh

        # If using default_factory, the defaults will work and the required argument can be ignored.
        goldman_sachs = Organization(name="Goldman Sachs")
        chase = Organization(name="JPMorgan Chase", my_dict={"test": "foobar"})

        session.add_all([goldman_sachs, chase])

        # Handling relationships and initializers can be tricky. Pydantic may raise errors
        # if these parameters are not set. However, both mapped_columns() and relationship()
        # have an `init=False` flag that ignores these fields during model initialization, but
        # they won't be set either.
        #
        # If we allow passing the entire object via initialization
        # (i.e., `User(..., organization=goldman_sachs)`), it may cause changes in
        # goldman_sachs's internal relationship management. This could lead to inconsistencies
        # and fatal errors as the state of goldman_sachs changes unexpectedly when
        # we pass it into an initializer of a related object (User).
        #
        # Setting the relationship after object creation has yielded the best results so far.
        #
        # Nothing we can't fix with custom initializers.
        user_alice = User(first_name="Alice", last_name="Johnson", organization=goldman_sachs)
        # Crash, because of pydantic swapping the init function on second time we call it. Read more: https://github.com/sqlalchemy/sqlalchemy/discussions/10243
        user_bob = User(first_name="Bob", last_name="Smith", organization=goldman_sachs)
        user_dave = User(first_name="Dave", last_name="Simpson", organization=chase)

        session.add_all([user_alice, user_bob, user_dave])

        await session.commit()

    # Let's read the records from thedb
    async with get_async_session(engine) as sesh:
        session: sqlalchemy.ext.asyncio.AsyncSession = sesh

        chase = (await session.execute(
            select(Organization).where(Organization.name.like("%chase%"))
        )).scalar_one()

        print(f"\nthe result of the chase.my_dict={chase.my_dict}\n")

        goldman_sachs = (await session.execute(
            select(Organization).where(Organization.name == "Goldman Sachs")
        )).scalar_one()

        # This is how you gather the relationships in an async way.
        # It will trigger a lazy loaded SELECT on the "users" table (see logs).
        users = await goldman_sachs.awaitable_attrs.users
        any_users = goldman_sachs.users

        print(f"\nthe result of the goldman_sachs.users={users}\n\n{any_users}")

        # Marshal the ORM model to Pydantic model
        goldman_sachs_pydantic = OrganizationPydantic.model_validate(goldman_sachs)
        print(f'chase={goldman_sachs_pydantic}')


    await engine.dispose()


if __name__ == "__main__":
    asyncio.run(async_main())

