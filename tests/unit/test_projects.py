from conftest import PROJECT

from helix_mcp_knowledge.application import KnowledgeApplication


def test_active_project_is_instance_scoped(config_path) -> None:
    first = KnowledgeApplication.from_config(config_path)
    second = KnowledgeApplication.from_config(config_path)
    first.set_active_project("example_project")
    assert first.get_active_project().project_id == "example_project"
    assert second.get_active_project().project_id is None


def test_clear_active_project(app) -> None:
    app.set_active_project("example_project")
    response = app.set_active_project(None)
    assert response.project_id is None
    assert response.source == "none"


def test_clear_active_project_overrides_configured_default(config_path) -> None:
    config_path.write_text(
        config_path.read_text(encoding="utf-8").replace(
            "default_project: null", "default_project: example_project"
        ),
        encoding="utf-8",
    )
    app = KnowledgeApplication.from_config(config_path)
    assert app.project_context.resolve().source == "configuration"

    app.set_active_project(None)

    assert app.project_context.resolve().project is None
    assert app.project_context.resolve().source == "none"


def test_disabled_project_is_persisted_but_not_selectable(config_path) -> None:
    (config_path.parent / "projects" / "legacy.yaml").write_text(
        PROJECT.format(project_id="legacy", name="Legacy", status="disabled", version="24.3"),
        encoding="utf-8",
    )
    application = KnowledgeApplication.from_config(config_path)
    with application.database.connect() as connection:
        row = connection.execute(
            "SELECT status FROM projects WHERE project_id = 'legacy'"
        ).fetchone()
    assert row["status"] == "disabled"
    assert all(project.id != "legacy" for project in application.list_projects().projects)
