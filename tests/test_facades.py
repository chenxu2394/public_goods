import public_goods.demo as demo_module
import public_goods.read_models as read_models
from public_goods.routes.admin import router


def test_public_facades_reexport_expected_helpers():
    assert callable(read_models.build_session_panel_context)
    assert callable(read_models.build_student_status_payload)
    assert callable(read_models.build_export_csv)

    assert callable(demo_module.create_demo_class)
    assert callable(demo_module.simulate_demo_current_round)
    assert callable(demo_module.simulate_demo_current_phase)


def test_admin_router_facade_includes_expected_paths():
    paths = {route.path for route in router.routes}

    assert "/admin/login" in paths
    assert "/admin" in paths
    assert "/admin/create" in paths
    assert "/admin/{session_id}" in paths
    assert "/admin/{session_id}/export" in paths
    assert "/admin/{session_id}/demo/run_current_phase" in paths
