from helix_mcp_knowledge.storage.automation import AutomationStore


def test_automation_lease_has_single_owner_and_supports_failover(app) -> None:
    first = AutomationStore(app.database)
    second = AutomationStore(app.database)

    assert first.acquire_or_renew("project-sync", "owner-1", 30) is True
    assert second.acquire_or_renew("project-sync", "owner-2", 30) is False
    assert first.renew("project-sync", "owner-1", 30) is True
    assert second.renew("project-sync", "owner-2", 30) is False

    first.release("project-sync", "owner-1")
    assert second.acquire_or_renew("project-sync", "owner-2", 30) is True


def test_expired_automation_lease_can_be_recovered(app) -> None:
    store = AutomationStore(app.database)
    assert store.acquire_or_renew("project-sync", "old-owner", 30) is True
    with app.database.connect() as connection:
        connection.execute(
            "UPDATE automation_leases SET expires_at = 0 WHERE lease_name = 'project-sync'"
        )
        connection.commit()

    assert store.acquire_or_renew("project-sync", "new-owner", 30) is True


def test_running_state_is_only_active_while_its_owner_has_a_live_lease(app) -> None:
    store = AutomationStore(app.database)
    state = {"status": "running", "owner_id": "worker"}

    assert store.running_state_is_active(state) is False
    assert store.acquire_or_renew("official-sync", "worker", 30)
    assert store.running_state_is_active(state) is True
    assert store.running_state_is_active({"status": "running"}) is False


def test_automation_state_round_trips(app) -> None:
    store = AutomationStore(app.database)
    expected = {"status": "ok", "result_counts": {"unchanged": 5}}
    store.update_state("project:example_project", expected)
    assert store.state("project:example_project") == expected


def test_automation_state_can_only_be_replaced_by_current_run_owner(app) -> None:
    store = AutomationStore(app.database)
    store.update_state("official-docs", {"status": "running", "owner_id": "one"})

    assert (
        store.update_state_if_current(
            "official-docs",
            owner_id="two",
            expected_status="running",
            state={"status": "ok", "owner_id": "two"},
        )
        is False
    )
    assert (
        store.update_state_if_current(
            "official-docs",
            owner_id="one",
            expected_status="running",
            state={"status": "pending", "owner_id": "one"},
        )
        is True
    )
    assert store.state("official-docs") == {"status": "pending", "owner_id": "one"}


def test_automation_state_can_be_patched_without_losing_concurrent_fields(app) -> None:
    store = AutomationStore(app.database)
    store.update_state(
        "official-docs",
        {"status": "running", "owner_id": "one", "progress": {"processed_items": 4}},
    )

    assert store.patch_state_if_current(
        "official-docs",
        expected_status="running",
        values={"cancel_requested": True},
    )
    assert not store.patch_state_if_current(
        "official-docs",
        owner_id="two",
        expected_status="running",
        values={"progress": {}},
    )

    assert store.state("official-docs") == {
        "status": "running",
        "owner_id": "one",
        "progress": {"processed_items": 4},
        "cancel_requested": True,
    }


def test_automation_state_atomic_merge_preserves_control_fields_and_defaults(app) -> None:
    store = AutomationStore(app.database)
    initial = store.patch_state(
        "optional-component",
        {"service_status": "starting"},
        defaults={"desired_enabled": True},
    )
    assert initial == {"desired_enabled": True, "service_status": "starting"}

    store.patch_state("optional-component", {"desired_enabled": False})
    completed = store.patch_state(
        "optional-component",
        {"service_status": "ready"},
        defaults={"desired_enabled": True},
    )

    assert completed == {"desired_enabled": False, "service_status": "ready"}
