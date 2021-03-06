#
# foris-controller
# Copyright (C) 2017 CZ.NIC, z.s.p.o. (http://www.nic.cz/)
#
# This program is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program; if not, write to the Free Software Foundation,
# Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301  USA
#

import pytest
import textwrap

from .conftest import cmdline_script_root
from foris_controller_testtools.fixtures import (
    infrastructure,
    uci_configs_init,
    start_buses,
    mosquitto_test,
    ubusd_test,
    init_script_result,
    only_backends,
    FILE_ROOT_PATH,
    UCI_CONFIG_DIR_PATH,
)
from foris_controller_testtools.utils import check_service_result, get_uci_module, FileFaker


@pytest.fixture(
    params=[(200, "free"), (200, "foreign"), (0, "unknown"), (404, "not_found"), (200, "owned")],
    ids=["free", "foreign", "unknown", "not_found", "owned"],
    scope="function",
)
def register_cmd(request, cmdline_script_root):
    status_code, status = request.param

    if not status_code:
        content = """\
            #!/bin/sh
            exit 1
        """
    else:
        content = """\
            #!/bin/sh
            cat <<-EOF
            status: %(status)s
            url: "https://some.page/${2:-en}/data?email=${1}&registration_code=XXXXXXX"
            code: %(code)d
            EOF
        """ % dict(
            code=status_code, status=status
        )

    with FileFaker(
        cmdline_script_root,
        "/usr/share/server-uplink/registered.sh",
        True,
        textwrap.dedent(content),
    ) as f:
        yield f, status


@pytest.fixture(scope="function")
def registration_code(request):
    content = "0000000B00009CD6"
    with FileFaker(
        FILE_ROOT_PATH,
        "/usr/share/server-uplink/registration_code",
        False,
        textwrap.dedent(content),
    ) as f:
        yield f, content


@pytest.mark.parametrize("code", ["cs", "nb_NO"])
def test_get_registered(code, uci_configs_init, infrastructure, start_buses):
    res = infrastructure.process_message(
        {
            "module": "data_collect",
            "action": "get_registered",
            "kind": "request",
            "data": {"email": "test@test.test", "language": code},
        }
    )
    assert "status" in res["data"].keys()


@pytest.mark.only_backends(["openwrt"])
@pytest.mark.parametrize("code", ["cs", "nb_NO"])
def test_get_registered_openwrt(
    cmdline_script_root,
    code,
    uci_configs_init,
    infrastructure,
    start_buses,
    register_cmd,
    registration_code,
):
    res = infrastructure.process_message(
        {
            "module": "data_collect",
            "action": "get_registered",
            "kind": "request",
            "data": {"email": "test@test.test", "language": code},
        }
    )
    _, status = register_cmd
    assert "status" in res["data"].keys()
    assert res["data"]["status"] == status
    assert status not in ["free", "foreign"] or "url" in res["data"]


def test_get_registered_errors(uci_configs_init, infrastructure, start_buses):
    res = infrastructure.process_message(
        {"module": "data_collect", "action": "get_registered", "kind": "request"}
    )

    assert "errors" in res
    assert "Incorrect input." in res["errors"][0]["description"]

    res = infrastructure.process_message(
        {"module": "data_collect", "action": "get_registered", "kind": "request", "data": {}}
    )
    assert "errors" in res
    assert "Incorrect input." in res["errors"][0]["description"]


def test_get(uci_configs_init, infrastructure, start_buses):
    res = infrastructure.process_message(
        {"module": "data_collect", "action": "get", "kind": "request"}
    )
    assert set(res["data"]) == {"agreed", "firewall_status", "ucollect_status"}


def test_set(uci_configs_init, init_script_result, infrastructure, start_buses):
    def set_agreed(agreed):
        filters = [("data_collect", "set")]
        old_notifications = infrastructure.get_notifications(filters=filters)
        res = infrastructure.process_message(
            {
                "module": "data_collect",
                "action": "set",
                "kind": "request",
                "data": {"agreed": agreed},
            }
        )
        assert res == {
            u"action": u"set",
            u"data": {u"result": True},
            u"kind": u"reply",
            u"module": u"data_collect",
        }
        notifications = infrastructure.get_notifications(old_notifications, filters=filters)
        assert notifications[-1] == {
            u"module": u"data_collect",
            u"action": u"set",
            u"kind": u"notification",
            u"data": {u"agreed": agreed},
        }
        res = infrastructure.process_message(
            {"module": "data_collect", "action": "get", "kind": "request"}
        )
        assert res["data"]["agreed"] is agreed

    set_agreed(True)
    set_agreed(False)
    set_agreed(True)
    set_agreed(False)


@pytest.mark.only_backends(["openwrt"])
def test_set_openwrt(uci_configs_init, init_script_result, infrastructure, start_buses):
    res = infrastructure.process_message(
        {"module": "data_collect", "action": "set", "kind": "request", "data": {"agreed": True}}
    )
    assert res == {
        u"action": u"set",
        u"data": {u"result": True},
        u"kind": u"reply",
        u"module": u"data_collect",
    }
    check_service_result("ucollect", "enable", clean=False)
    check_service_result("ucollect", "restart")

    res = infrastructure.process_message(
        {"module": "data_collect", "action": "set", "kind": "request", "data": {"agreed": False}}
    )
    assert res == {
        u"action": u"set",
        u"data": {u"result": True},
        u"kind": u"reply",
        u"module": u"data_collect",
    }
    check_service_result("ucollect", "disable", clean=False)
    check_service_result("ucollect", "stop")


def test_get_honeypots(infrastructure, start_buses):
    res = infrastructure.process_message(
        {"module": "data_collect", "action": "get_honeypots", "kind": "request"}
    )
    assert {"minipots", "log_credentials"} == set(res["data"].keys())


def test_set_honeypots(infrastructure, init_script_result, start_buses):
    filters = [("data_collect", "set_honeypots")]

    def set_honeypots(result):
        notifications = infrastructure.get_notifications(filters=filters)
        res = infrastructure.process_message(
            {
                "module": "data_collect",
                "action": "set_honeypots",
                "kind": "request",
                "data": {
                    "minipots": {
                        "23tcp": result,
                        "2323tcp": result,
                        "80tcp": result,
                        "3128tcp": result,
                        "8123tcp": result,
                        "8080tcp": result,
                    },
                    "log_credentials": result,
                },
            }
        )
        assert res == {
            u"action": u"set_honeypots",
            u"data": {u"result": True},
            u"kind": u"reply",
            u"module": u"data_collect",
        }
        notifications = infrastructure.get_notifications(notifications, filters=filters)
        assert notifications[-1] == {
            u"module": u"data_collect",
            u"action": u"set_honeypots",
            u"kind": u"notification",
            u"data": {
                "minipots": {
                    "23tcp": result,
                    "2323tcp": result,
                    "80tcp": result,
                    "3128tcp": result,
                    "8123tcp": result,
                    "8080tcp": result,
                },
                "log_credentials": result,
            },
        }
        res = infrastructure.process_message(
            {"module": "data_collect", "action": "get_honeypots", "kind": "request"}
        )
        assert res == {
            u"module": u"data_collect",
            u"action": u"get_honeypots",
            u"kind": u"reply",
            u"data": {
                "minipots": {
                    "23tcp": result,
                    "2323tcp": result,
                    "80tcp": result,
                    "3128tcp": result,
                    "8123tcp": result,
                    "8080tcp": result,
                },
                "log_credentials": result,
            },
        }

    set_honeypots(True)
    set_honeypots(False)
    set_honeypots(True)
    set_honeypots(False)


@pytest.mark.only_backends(["openwrt"])
def test_set_honeypots_service_restart(
    uci_configs_init, init_script_result, infrastructure, start_buses
):
    res = infrastructure.process_message(
        {
            "module": "data_collect",
            "action": "set_honeypots",
            "kind": "request",
            "data": {
                "minipots": {
                    "23tcp": True,
                    "2323tcp": False,
                    "80tcp": True,
                    "3128tcp": False,
                    "8123tcp": True,
                    "8080tcp": False,
                },
                "log_credentials": False,
            },
        }
    )
    assert res == {
        u"action": u"set_honeypots",
        u"data": {u"result": True},
        u"kind": u"reply",
        u"module": u"data_collect",
    }
    check_service_result("ucollect", "restart", True)


@pytest.mark.only_backends(["openwrt"])
def test_set_agreed_uci(uci_configs_init, init_script_result, infrastructure, start_buses):
    uci = get_uci_module(infrastructure.name)

    res = infrastructure.process_message(
        {"module": "data_collect", "action": "set", "kind": "request", "data": {"agreed": True}}
    )
    assert res == {
        u"action": u"set",
        u"data": {u"result": True},
        u"kind": u"reply",
        u"module": u"data_collect",
    }
    with uci.UciBackend(UCI_CONFIG_DIR_PATH) as backend:
        data = backend.read()

    assert uci.parse_bool(uci.get_option_named(data, "foris", "eula", "agreed_collect", "0"))

    res = infrastructure.process_message(
        {"module": "data_collect", "action": "set", "kind": "request", "data": {"agreed": False}}
    )
    assert res == {
        u"action": u"set",
        u"data": {u"result": True},
        u"kind": u"reply",
        u"module": u"data_collect",
    }

    with uci.UciBackend(UCI_CONFIG_DIR_PATH) as backend:
        data = backend.read()

    assert not uci.parse_bool(uci.get_option_named(data, "foris", "eula", "agreed_collect", "0"))
