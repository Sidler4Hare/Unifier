"""B-stage project/handler routing over real temporary files and FakeRuntime."""

from contextlib import redirect_stdout
from dataclasses import replace
from io import StringIO
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / "src"), str(ROOT / "tests")]

from multilinux import cli
from multilinux.errors import MultilinuxError
from multilinux import file_routes as file_routes_module
from multilinux.file_routes import FileRoutes
from test_desktop import SESSION, desktop_service


def ready(root: Path, *, on_demand=True, root_policy="read-write",
          project_policy="read-write", save_capability="explicit-path",
          argv_template=None, app_argv=None):
    project = root / "project"
    project.mkdir()
    document = project / "零件 1.FCStd"
    document.write_text("model\n", encoding="utf-8")
    service, runtime = desktop_service(root)
    service.create(
        "arch", None, [str(project)], desktop=True, session=SESSION,
        share_label_policy="preserve-host")
    service.add_app(
        "arch", "freecad", "/usr/bin/freecad",
        app_argv or ["/usr/bin/freecad"], workdir=str(project), on_demand=on_demand)
    routes = FileRoutes(root, service)
    root_record = routes.roots.add(str(project), access_policy=root_policy)
    handler = routes.add_handler(
        "freecad", intent="edit", save_capability=save_capability,
        alias="freecad-edit", argv_template=argv_template)
    project_record = routes.add_project(
        root_record["root_uuid"], "arch", alias="demo-project",
        default_save_root=str(project), access_policy=project_policy,
        intent_handlers={"edit": handler["handler_uuid"]})
    return routes, service, runtime, project_record, root_record, handler, document


def _plan(routes, path, intent="edit"):
    return routes.plan(str(path), intent)


class PlanContract(unittest.TestCase):
    def test_plan_carries_bindings_argv_cwd_scope_and_save_contract(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, runtime, project, root, handler, document = ready(Path(temporary))
            before = list(runtime.events)
            plan = _plan(routes, document)
            self.assertEqual(plan["provider"], "environment-app")
            self.assertEqual(plan["intent"], "edit")
            self.assertEqual(plan["project_uuid"], project["project_uuid"])
            self.assertEqual(plan["root_uuid"], root["root_uuid"])
            self.assertEqual(plan["handler_uuid"], handler["handler_uuid"])
            self.assertEqual(plan["environment_id"], "arch")
            self.assertEqual(plan["generation"], 1)
            self.assertEqual(plan["container_id"], "cid-arch")
            self.assertEqual(plan["argv"], ["/usr/bin/freecad", str(document)])
            self.assertEqual(plan["cwd"], str(document.parent))
            self.assertEqual(plan["default_save_root"], str(document.parent))
            self.assertEqual(plan["save_capability"], "explicit-path")
            self.assertEqual(plan["save_as_default"], "unknown")
            self.assertFalse(plan["default_save_enforced"])
            self.assertEqual(plan["observed_version"]["ino"], document.stat().st_ino)
            self.assertEqual(plan["access_scope"]["mode"], "environment-shared-static-mounts")
            self.assertIn("share every static mount", plan["access_scope"]["warning"])
            self.assertEqual(plan["access_scope"]["policy_enforcement"], "routing-only")
            self.assertEqual(plan["access_scope"]["mappings"]["file"]["path"], str(document))
            selected = plan["access_scope"]["selected_mount"]
            self.assertEqual(selected["host"], str(document.parent))
            self.assertTrue(selected["same_absolute_path"])
            self.assertEqual(plan["execution"], {
                "mode": "argv-array", "shell": False, "on_demand": True,
                "revalidate_on_open": True})
            self.assertEqual(runtime.events, before)

    def test_argv_template_replaces_only_the_file_token(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary),
                argv_template=["/usr/bin/freecad", "--open", "{file}"])
            plan = _plan(routes, document)
            self.assertEqual(plan["argv"], ["/usr/bin/freecad", "--open", str(document)])

    def test_special_filename_is_one_argv_element_and_standalone_token_is_required(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, _project, _root, _handler, _document = ready(
                Path(temporary))
            special = _document.parent / '零件 "a";b.FCStd'
            special.write_text("special\n", encoding="utf-8")
            plan = routes.plan(str(special), "edit")
            self.assertEqual(plan["argv"][-1], str(special))
            self.assertNotIn("sh", plan["argv"][:1])
            with self.assertRaisesRegex(MultilinuxError, "standalone"):
                routes.add_handler(
                    "freecad", intent="edit", save_capability="explicit-path",
                    alias="embedded", argv_template=["/usr/bin/freecad", "--file={file}"])

    def test_open_uses_literal_argv_and_on_demand_lease(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            runtime.events.clear()
            # This is the detached worker's own entry point: it holds the lease.
            result = routes.worker_open(str(document), "edit")
            self.assertEqual(result.returncode, 0)
            starts = [event for event in runtime.events if event[0] == "start"]
            stops = [event for event in runtime.events if event[0] == "stop"]
            app_execs = [event for event in runtime.events
                         if event[0] == "exec" and event[2][0] == "/usr/bin/freecad"]
            self.assertEqual(len(starts), 1)
            self.assertEqual(len(stops), 1)
            self.assertEqual(len(app_execs), 1)
            _kind, _name, argv, workdir, _env, capture = app_execs[0]
            self.assertEqual(argv, ("/usr/bin/freecad", str(document)))
            self.assertEqual(workdir, str(document.parent))
            self.assertFalse(capture)
            self.assertNotIn("/bin/sh", argv)

    def test_missing_project_then_missing_handler_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            document = project / "a.txt"
            document.write_text("x", encoding="utf-8")
            service, _runtime = desktop_service(root)
            service.create("arch", None, [str(project)], desktop=True, session=SESSION)
            service.add_app("arch", "freecad", "/usr/bin/freecad",
                            ["/usr/bin/freecad"], workdir=str(project))
            routes = FileRoutes(root, service)
            routes.roots.add(str(project))
            with self.assertRaisesRegex(MultilinuxError, "no project"):
                routes.plan(str(document), "edit")
            handler = routes.add_handler("freecad", intent="edit",
                                         save_capability="explicit-path")
            routes.add_project(routes.roots.list()[0]["root_uuid"], "arch",
                               intent_handlers={})
            with self.assertRaisesRegex(MultilinuxError, "no edit handler"):
                routes.plan(str(document), "edit")
            self.assertEqual(len(routes.list_handlers()), 1)


class FailurePaths(unittest.TestCase):
    def test_read_only_project_or_root_refuses_edit(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary), project_policy="read-only")
            with self.assertRaisesRegex(MultilinuxError, "project policy"):
                routes.plan(str(document), "edit")

        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary), root_policy="read-only")
            with self.assertRaisesRegex(MultilinuxError, "data root policy"):
                routes.plan(str(document), "edit")

    def test_readonly_policy_view_rejects_writable_runtime_mounts(self):
        for root_policy, project_policy in (("read-write", "read-only"),
                                           ("read-only", "read-write")):
            with self.subTest(root_policy=root_policy, project_policy=project_policy), \
                 tempfile.TemporaryDirectory() as temporary:
                routes, service, runtime, project, root, edit, document = ready(
                    Path(temporary), root_policy=root_policy,
                    project_policy=project_policy)
                view = routes.add_handler("freecad", intent="view",
                                          save_capability="unknown")
                routes.remove_project(project["project_uuid"])
                project = routes.add_project(
                    root["root_uuid"], "arch",
                    default_save_root=str(document.parent),
                    access_policy=project_policy,
                    intent_handlers={"edit": edit["handler_uuid"],
                                     "view": view["handler_uuid"]})
                runtime.events.clear()
                with self.assertRaisesRegex(MultilinuxError, "read-only policy"):
                    routes.plan(str(document), "view")
                self.assertEqual(runtime.events, [])

                # With every used mount actually read-only, the readonly
                # policy is truthful and view planning is allowed.
                record = service.store.load("arch")
                for share in record["shared_mounts"]:
                    share["options"] = "ro"
                service.store.save(record, None)
                plan = routes.plan(str(document), "view")
                self.assertTrue(plan["access_scope"]["runtime_read_only"])

    def test_default_save_root_nested_root_and_symlink_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, project, root, handler, document = ready(
                Path(temporary))
            nested = document.parent / "nested"
            nested.mkdir()
            routes.roots.add(str(nested), access_policy="read-only")
            routes.remove_project(project["project_uuid"])

            with self.assertRaisesRegex(MultilinuxError, "different registered data root"):
                routes.add_project(
                    root["root_uuid"], "arch", default_save_root=str(nested),
                    intent_handlers={})
            link = document.parent / "save-link"
            os.symlink(nested, link)
            with self.assertRaisesRegex(MultilinuxError, "symbolic links"):
                routes.add_project(
                    root["root_uuid"], "arch", default_save_root=str(link),
                    intent_handlers={})
            project = routes.add_project(
                root["root_uuid"], "arch", default_save_root=str(document.parent),
                intent_handlers={"edit": handler["handler_uuid"]})
            with routes.projects.collection.locked(True):
                records = routes.projects.collection.read_unlocked()
                records[0]["default_save_root"] = str(nested)
                routes.projects.collection.write_unlocked(records)
            with self.assertRaisesRegex(MultilinuxError, "different registered data root"):
                routes.plan(str(document), "edit")
            self.assertEqual(project["project_uuid"], records[0]["project_uuid"])

    def test_shadowing_container_mount_and_child_readonly_mount_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, runtime, project, root, edit_handler, document = ready(
                Path(temporary))
            nested = document.parent / "nested"
            nested.mkdir()
            target = nested / "doc.txt"
            target.write_text("target\n", encoding="utf-8")
            outside = Path(temporary) / "outside"
            outside.mkdir()
            (outside / "doc.txt").write_text("decoy\n", encoding="utf-8")
            view_handler = routes.add_handler(
                "freecad", intent="view", save_capability="unknown")
            routes.remove_project(project["project_uuid"])
            routes.add_project(
                root["root_uuid"], "arch", default_save_root=str(document.parent),
                intent_handlers={"edit": edit_handler["handler_uuid"],
                                 "view": view_handler["handler_uuid"]})

            record = service.store.load("arch")
            record["shared_mounts"].append({
                "host": str(outside), "container": str(nested), "options": ""})
            service.store.save(record, None)
            runtime.events.clear()
            with self.assertRaisesRegex(MultilinuxError, "shadowed"):
                routes.plan(str(target), "edit")
            self.assertEqual(runtime.events, [])

            record = service.store.load("arch")
            record["shared_mounts"][-1] = {
                "host": str(nested), "container": str(nested), "options": "ro"}
            service.store.save(record, None)
            runtime.events.clear()
            with self.assertRaisesRegex(MultilinuxError, "read-only"):
                routes.plan(str(target), "edit")
            self.assertEqual(runtime.events, [])
            view = routes.plan(str(target), "view")
            self.assertTrue(view["access_scope"]["any_used_mount_read_only"])

    def test_default_save_root_outside_project_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, _service, _runtime, project, _root, _handler, _document = ready(root)
            outside = root / "outside"
            outside.mkdir()
            with self.assertRaisesRegex(MultilinuxError, "inside the project root"):
                routes.add_project(
                    routes.roots.list()[0]["root_uuid"], "arch",
                    default_save_root=str(outside), intent_handlers={})
            self.assertEqual(len(routes.list_projects()), 1)

    def test_missing_or_different_environment_share_refuses_route(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            record = service.store.load("arch")
            record["shared_mounts"] = []
            service.store.save(record, None)
            with self.assertRaisesRegex(MultilinuxError, "no static mount|does not mount"):
                routes.plan(str(document), "edit")

        with tempfile.TemporaryDirectory() as temporary:
            routes, service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            record = service.store.load("arch")
            for share in record["shared_mounts"]:
                share["container"] = share["host"] + "-other"
            service.store.save(record, None)
            with self.assertRaisesRegex(MultilinuxError, "no static mount|does not mount"):
                routes.plan(str(document), "edit")

    def test_root_replacement_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            routes, _service, _runtime, _project, _root, _handler, document = ready(root)
            moved = root / "old-project"
            document.parent.rename(moved)
            document.parent.mkdir()
            (document.parent / document.name).write_text("new", encoding="utf-8")
            with self.assertRaisesRegex(MultilinuxError, "replaced"):
                routes.plan(str(document), "edit")

    def test_environment_generation_or_container_instance_change_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            record = service.store.load("arch")
            record["generation"] = 2
            service.store.save(record, None)
            with self.assertRaisesRegex(MultilinuxError, "stale|binding"):
                routes.plan(str(document), "edit")

    def test_app_execution_configuration_change_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            launch = service.show_app("freecad")
            launch["argv"] = ["/usr/bin/freecad", "--changed"]
            service.store.save_launch(launch)
            with self.assertRaisesRegex(MultilinuxError, "execution configuration changed"):
                routes.plan(str(document), "edit")

    def test_handler_same_alias_rebuild_does_not_replace_project_authorization(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, _project, _root, handler, document = ready(
                Path(temporary))
            routes.remove_handler(handler["handler_uuid"])
            replacement = routes.add_handler(
                "freecad", intent="edit", save_capability="explicit-path",
                alias="freecad-edit")
            self.assertNotEqual(replacement["handler_uuid"], handler["handler_uuid"])
            with self.assertRaisesRegex(MultilinuxError, "not registered"):
                routes.plan(str(document), "edit")

    def test_handler_registration_refuses_a_legacy_launch(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            project = root / "project"
            project.mkdir()
            service, _runtime = desktop_service(root)
            service.create("arch", None, [str(project)], desktop=True, session=SESSION)
            service.add_app("arch", "freecad", "/usr/bin/freecad",
                            ["/usr/bin/freecad"], workdir=str(project))
            launch = service.show_app("freecad")
            launch.pop("environment_binding")
            launch.pop("environment_uuid", None)
            launch.pop("generation", None)
            launch.pop("container_id", None)
            launch["schema_version"] = 1
            service.store.save_launch(launch)
            routes = FileRoutes(root, service)
            routes.roots.add(str(project))
            with self.assertRaisesRegex(MultilinuxError, "legacy launch"):
                routes.add_handler("freecad", intent="edit",
                                   save_capability="explicit-path")
            self.assertEqual(routes.list_handlers(), [])

    def test_plan_is_not_authority_when_binding_changes_before_open(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            routes.plan(str(document), "edit")
            launch = service.show_app("freecad")
            launch["env"] = {**launch["env"], "MULTILINUX_CHANGED": "1"}
            service.store.save_launch(launch)
            runtime.events.clear()
            with self.assertRaisesRegex(MultilinuxError, "execution configuration changed"):
                routes.open(str(document), "edit")
            self.assertEqual(runtime.events, [])

    def test_same_name_identical_app_recreation_is_refused_by_instance_uuid(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            first = service.show_app("freecad")
            service.remove_app("freecad")
            service.add_app("arch", "freecad", "/usr/bin/freecad",
                            ["/usr/bin/freecad"], workdir=str(document.parent),
                            on_demand=True)
            second = service.show_app("freecad")
            self.assertNotEqual(first["app_instance_uuid"], second["app_instance_uuid"])
            self.assertEqual(first["argv"], second["argv"])
            self.assertEqual(first["env"], second["env"])
            with self.assertRaisesRegex(MultilinuxError, "instance"):
                routes.plan(str(document), "edit")

    def test_post_wait_app_change_is_refused_before_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, runtime, _project, _root, _handler, document = ready(
                Path(temporary), on_demand=False)
            original_wait = service._wait_while_stopping

            def wait_then_change(record):
                info = original_wait(record)
                launch = service.show_app("freecad")
                launch["env"] = {**launch["env"], "MULTILINUX_AFTER_WAIT": "1"}
                service.store.save_launch(launch)
                return info

            runtime.events.clear()
            with patch.object(service, "_wait_while_stopping", wait_then_change):
                with self.assertRaisesRegex(MultilinuxError, "execution configuration"):
                    routes.open(str(document), "edit")
            self.assertEqual(runtime.events, [])

    def test_post_wait_environment_instance_change_is_refused_before_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, runtime, _project, _root, _handler, document = ready(
                Path(temporary), on_demand=False)
            original_wait = service._wait_while_stopping

            def wait_then_change(record):
                info = original_wait(record)
                latest = service.store.load("arch")
                latest["generation"] = latest["generation"] + 1
                service.store.save(latest, None)
                return info

            runtime.events.clear()
            with patch.object(service, "_wait_while_stopping", wait_then_change):
                with self.assertRaisesRegex(MultilinuxError, "stale|binding"):
                    routes.open(str(document), "edit")
            self.assertEqual(runtime.events, [])

    def test_normal_wait_still_starts_and_executes(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, runtime, _project, _root, _handler, document = ready(
                Path(temporary), on_demand=False)
            original_wait = service._wait_while_stopping

            def wait_only(record):
                return original_wait(record)

            runtime.events.clear()
            with patch.object(service, "_wait_while_stopping", wait_only):
                result = routes.open(str(document), "edit")
            self.assertEqual(result.returncode, 0)
            self.assertTrue(any(event[0] == "start" for event in runtime.events))
            self.assertTrue(any(
                event[0] == "exec" and event[2][0] == "/usr/bin/freecad"
                for event in runtime.events))

    def test_dispatch_rechecks_expected_app_configuration_before_start(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            context = routes._resolve(str(document), "edit")
            launch = service.show_app("freecad")
            launch["env"] = {**launch["env"], "MULTILINUX_CHANGED": "1"}
            service.store.save_launch(launch)
            runtime.events.clear()
            with self.assertRaisesRegex(MultilinuxError, "execution configuration changed"):
                routes._execute(context)
            self.assertEqual(runtime.events, [])

    def test_unsupported_provider_and_bad_file_argument_are_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            with self.assertRaisesRegex(MultilinuxError, "provider"):
                routes.add_handler("freecad", intent="edit", provider="host-exec",
                                   save_capability="explicit-path")
            with self.assertRaisesRegex(MultilinuxError, "standalone"):
                routes.add_handler("freecad", intent="edit",
                                   save_capability="explicit-path",
                                   argv_template=["/usr/bin/freecad", "no-token"])


class RegistryContract(unittest.TestCase):
    def test_duplicate_project_and_handler_records_are_refused_on_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, project, _root, handler, _document = ready(
                Path(temporary))
            for registry, key in ((routes.projects, "project_uuid"),
                                  (routes.handlers, "handler_uuid")):
                payload = json.loads(registry.collection.path.read_text(encoding="utf-8"))
                duplicate = json.loads(json.dumps(payload["records"][0]))
                if key == "project_uuid":
                    duplicate["alias"] = "other-project"
                else:
                    duplicate["alias"] = "other-handler"
                payload["records"].append(duplicate)
                registry.collection.path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                with self.assertRaisesRegex(MultilinuxError, f"duplicate {key}"):
                    registry.list()

    def test_duplicate_alias_rejected_on_load(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, project, _root, handler, _document = ready(
                Path(temporary))
            for registry, uuid_key, new_uuid in (
                    (routes.projects, "project_uuid",
                     "00000000-0000-4000-8000-000000000001"),
                    (routes.handlers, "handler_uuid",
                     "00000000-0000-4000-8000-000000000002")):
                payload = json.loads(registry.collection.path.read_text(encoding="utf-8"))
                duplicate = json.loads(json.dumps(payload["records"][0]))
                duplicate[uuid_key] = new_uuid
                payload["records"].append(duplicate)
                registry.collection.path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")
                with self.assertRaisesRegex(MultilinuxError, "duplicate alias"):
                    registry.list()
                # Restore the original registry for the next case.
                payload["records"].pop()
                registry.collection.path.write_text(
                    json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_direct_api_wrong_types_raise_product_errors(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, project, root, _handler, document = ready(
                Path(temporary))
            with self.assertRaises(MultilinuxError):
                routes.plan(str(document), [])
            routes.remove_project(project["project_uuid"])
            with self.assertRaises(MultilinuxError):
                routes.add_project(root["root_uuid"], "arch", intent_handlers=[])
            with self.assertRaises(MultilinuxError):
                routes.add_project(root["root_uuid"], "arch", access_policy={})


class FileSupervisor(unittest.TestCase):
    def test_on_demand_open_spawns_detached_worker_and_returns_its_status(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            calls = []

            class Worker:
                def wait(self):
                    calls.append("wait")
                    return 17

            def popen(command, **kwargs):
                calls.append((list(command), dict(kwargs)))
                return Worker()

            original = file_routes_module.subprocess.Popen
            file_routes_module.subprocess.Popen = popen
            try:
                result = routes.open(str(document), "edit")
            finally:
                file_routes_module.subprocess.Popen = original
            self.assertEqual(result.returncode, 17)
            self.assertEqual(calls[-1], "wait")
            command, kwargs = calls[0]
            self.assertEqual(command[1:4], [str(ROOT / "tools/ml.py"), "file", "_worker"])
            self.assertIn("--path", command)
            self.assertIn(str(document), command)
            self.assertTrue(kwargs["start_new_session"])
            self.assertEqual(kwargs["env"][file_routes_module.FILE_WORKER_MARKER], "1")

    def test_worker_failure_before_lease_has_zero_runtime_side_effects(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, runtime, _project, _root, _handler, document = ready(
                Path(temporary))
            launch = service.show_app("freecad")
            launch["argv"] = ["/usr/bin/freecad", "--changed"]
            service.store.save_launch(launch)
            runtime.events.clear()
            with self.assertRaisesRegex(MultilinuxError, "execution configuration changed"):
                routes.worker_open(str(document), "edit")
            self.assertEqual(runtime.events, [])

class RegistrationAndReuse(unittest.TestCase):
    def test_remove_project_and_handler_keep_data_app_environment(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, _runtime, project, root, handler, document = ready(
                Path(temporary))
            removed_handler = routes.remove_handler(handler["handler_uuid"])
            removed_project = routes.remove_project(project["project_uuid"])
            self.assertEqual(removed_handler["handler_uuid"], handler["handler_uuid"])
            self.assertEqual(removed_project["project_uuid"], project["project_uuid"])
            self.assertTrue(document.is_file())
            self.assertTrue(document.parent.is_dir())
            self.assertTrue(service.store.exists("arch"))
            self.assertEqual(service.show_app("freecad")["id"], "freecad")
            self.assertEqual(routes.roots.show(root["root_uuid"])["root_uuid"],
                             root["root_uuid"])
            with self.assertRaisesRegex(MultilinuxError, "no project"):
                routes.plan(str(document), "edit")

    def test_environment_deletion_keeps_data_root_and_project_registration(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, _runtime, project, root, _handler, document = ready(
                Path(temporary))
            service.remove("arch")
            self.assertTrue(document.is_file())
            self.assertTrue(document.parent.is_dir())
            self.assertEqual(routes.show_project(project["project_uuid"])["project_uuid"],
                             project["project_uuid"])
            self.assertEqual(routes.roots.show(root["root_uuid"])["root_uuid"],
                             root["root_uuid"])
            with self.assertRaises(MultilinuxError):
                routes.plan(str(document), "edit")

    def test_two_handlers_share_one_environment_instance(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, service, _runtime, project, _root, edit_handler, document = ready(
                Path(temporary))
            service.add_app("arch", "freecad-view", "/usr/bin/freecad",
                            ["/usr/bin/freecad", "--view"], workdir=str(document.parent))
            view = routes.add_handler("freecad-view", intent="view",
                                      save_capability="unknown")
            # Re-register the project with both intent handlers.
            routes.remove_project(project["project_uuid"])
            project = routes.add_project(
                _root["root_uuid"], "arch", alias="demo-project",
                default_save_root=str(document.parent),
                intent_handlers={"edit": edit_handler["handler_uuid"],
                                 "view": view["handler_uuid"]})
            edit_plan = routes.plan(str(document), "edit")
            view_plan = routes.plan(str(document), "view")
            self.assertEqual(edit_plan["environment_uuid"], view_plan["environment_uuid"])
            self.assertEqual(edit_plan["container_id"], view_plan["container_id"])
            self.assertEqual(edit_plan["project_uuid"], project["project_uuid"])
            self.assertEqual(edit_plan["root_uuid"], view_plan["root_uuid"])
            self.assertEqual(len([event for event in service.runtime.containers]), 1)

    def test_cli_project_and_handler_commands(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, _runtime, project, root, handler, document = ready(
                Path(temporary))
            original = cli.routing_factory
            cli.routing_factory = lambda: routes
            try:
                def run(*argv):
                    stdout = StringIO()
                    with redirect_stdout(stdout):
                        code = cli.main(list(argv))
                    return code, stdout.getvalue()

                code, output = run("handler", "list")
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(output)[0]["handler_uuid"],
                                 handler["handler_uuid"])
                code, output = run("handler", "show", "freecad-edit")
                self.assertEqual((code, json.loads(output)["intent"]), (0, "edit"))
                code, output = run("project", "show", "demo-project")
                self.assertEqual((code, json.loads(output)["project_uuid"]),
                                 (0, project["project_uuid"]))
                code, _output = run("project", "remove", "demo-project")
                self.assertEqual(code, 0)
                code, output = run(
                    "project", "add", "--root", root["root_uuid"],
                    "--environment", "arch", "--alias", "demo-project",
                    "--default-save-root", str(document.parent),
                    "--handler", f"edit={handler['handler_uuid']}")
                self.assertEqual(code, 0)
                rebuilt = json.loads(output)
                self.assertNotEqual(rebuilt["project_uuid"], project["project_uuid"])
                self.assertEqual(rebuilt["intent_handlers"]["edit"],
                                 handler["handler_uuid"])
                self.assertEqual(run("handler", "remove", "freecad-edit")[0], 0)
                self.assertEqual(run("project", "remove", "demo-project")[0], 0)
            finally:
                cli.routing_factory = original

    def test_cli_plan_and_open_return_codes(self):
        with tempfile.TemporaryDirectory() as temporary:
            routes, _service, runtime, _project, _root, _handler, document = ready(
                Path(temporary), on_demand=False)
            original = cli.routing_factory
            cli.routing_factory = lambda: routes
            try:
                stdout = StringIO()
                with redirect_stdout(stdout):
                    code = cli.main(["file", "plan", "--path", str(document)])
                self.assertEqual(code, 0)
                self.assertEqual(json.loads(stdout.getvalue())["argv"],
                                 ["/usr/bin/freecad", str(document)])
                runtime.next_code = 23
                code = cli.main(["file", "open", "--path", str(document)])
                self.assertEqual(code, 23)
            finally:
                cli.routing_factory = original


if __name__ == "__main__":
    unittest.main()
