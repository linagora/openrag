from api.schemas.admin.users import UserCreate, UserPublic, UserUpdate
from api.schemas.user.chat import OpenAIChatCompletionRequest, OpenAICompletionRequest, OpenAIMessage
from api.schemas.user.search import SearchRequest


def test_user_schemas_import_from_api_package():
    created = UserCreate(display_name="Alice", email=None)
    updated = UserUpdate(display_name="Bob")
    public = UserPublic(id=1, display_name="Alice", external_user_id=None, email=None, is_admin=False, created_at=None)

    assert created.email is None
    assert updated.display_name == "Bob"
    assert public.file_quota is None


def test_openai_schemas_preserve_existing_defaults():
    request = OpenAIChatCompletionRequest(messages=[OpenAIMessage(role="user", content="hello")])
    completion = OpenAICompletionRequest(prompt="hello")

    assert request.temperature == 0.3
    assert request.top_p == 1.0
    assert request.stream is False
    assert completion.best_of == 1


def test_search_schema_preserves_existing_defaults():
    request = SearchRequest(query="hello")

    assert request.query == "hello"
    assert request.top_k == 5
