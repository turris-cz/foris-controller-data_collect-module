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

import re
import logging

from foris_controller.app import app_info
from foris_controller.exceptions import BackendCommandFailed
from foris_controller_backends.cmdline import BaseCmdLine
from foris_controller_backends.files import BaseFile
from foris_controller_backends.services import OpenwrtServices
from foris_controller_backends.uci import (
    UciBackend, UciRecordNotFound, parse_bool, get_option_named, store_bool
)
from foris_controller.utils import readlock, RWLock


logger = logging.getLogger(__name__)


class RegisteredCmds(BaseCmdLine):

    def _query_registered(self, email, language):
        # get registration code
        from foris_controller_backends.about import ServerUplinkFiles
        registration_code = ServerUplinkFiles().get_registration_number()
        if not registration_code:
            # failed to obtain registration code
            return {"status": "unknown"}
        retcode, stdout, _ = self._run_command(
            "/usr/share/server-uplink/registered.sh",
            email, language
        )
        stdout = stdout.decode()
        if not retcode == 0:
            # cmd failed (e.g. connection failed)
            return {"status": "unknown"}

        # code field should be present
        code_re = re.search(r"code: ([0-9]+)", stdout)
        http_code = int(code_re.group(1))
        if http_code != 200:
            return {"status": "not_found"}

        # status should be present
        status_re = re.search(r"status: (\w+)", stdout)
        status = status_re.group(1)

        if status == "owned":
            return {"status": status}
        elif status in ["free", "foreign"]:
            url_re = re.search(r"url: ([^\s]+)", stdout)
            url = url_re.group(1)
            return {
                "status": status, "url": url,
                "registration_number": registration_code,
            }

        return {"status": "unknown"}

    def get_registered(self, email, language):
        """ Returns registration status
        :param email: email which will be used in the server query
        :type email: str
        :param language: language which will be used in the server query (en/cs)
        :type language: str

        :returns: registration status and sometimes registration url
        :rtype: dict
        """
        res = self._query_registered(email, language)

        if res["status"] == "not_found":
            # Try to update registration code first
            try:
                self._run_command_and_check_retval(
                    ["/usr/share/server-uplink/registration_code.sh"], 0)
            except BackendCommandFailed:
                return {"status": "not_found"}
            res = self._query_registered(email, language)

        return res


class DataCollectUci(object):
    MINIPOTS = {"23tcp", "2323tcp", "8123tcp", "8080tcp", "80tcp", "3128tcp"}
    LOG_CREDENTIALS_DEFAULT = False

    def get_agreed(self):
        with UciBackend() as backend:
            foris_data = backend.read("foris")

        try:
            return parse_bool(get_option_named(foris_data, "foris", "eula", "agreed_collect"))
        except UciRecordNotFound:
            return False

    def set_agreed(self, agreed):
        with UciBackend() as backend:
            backend.add_section("foris", "config", "eula")
            backend.set_option("foris", "eula", "agreed_collect", store_bool(agreed))

        with OpenwrtServices() as services:
            if agreed:
                services.enable("ucollect")
                services.restart("ucollect")
            else:
                services.disable("ucollect")
                services.stop("ucollect")

        return True

    def get_honeypots(self):
        with UciBackend() as backend:
            ucollect_data = backend.read("ucollect")

        try:
            log_credentials = parse_bool(get_option_named(
                ucollect_data, "ucollect", "fakes", "log_credentials",
                store_bool(self.LOG_CREDENTIALS_DEFAULT)
            ))
        except UciRecordNotFound:
            log_credentials = self.LOG_CREDENTIALS_DEFAULT

        # all minipots seems to be enabled by default
        minipots = {e: True for e in self.MINIPOTS}
        try:
            disabled_minipots = get_option_named(
                ucollect_data, "ucollect", "fakes", "disable", [])
            for disabled in set(disabled_minipots).intersection(self.MINIPOTS):
                minipots[disabled] = False

        except UciRecordNotFound:
            pass

        return {
            "log_credentials": log_credentials,
            "minipots": minipots,
        }

    def set_honeypots(self, honeypot_data):
        disabled_minipots = [k for k, v in honeypot_data["minipots"].items() if not v]

        with UciBackend() as backend:
            backend.add_section("ucollect", "fakes", "fakes")
            backend.replace_list("ucollect", "fakes", "disable", disabled_minipots)
            backend.set_option(
                "ucollect", "fakes", "log_credentials",
                store_bool(honeypot_data["log_credentials"]),
            )

        with OpenwrtServices() as services:
            services.restart("ucollect")

        return True


class SendingFiles(BaseFile):
    FW_PATH = "/tmp/firewall-turris-status.txt"
    UC_PATH = "/tmp/ucollect-status"
    file_lock = RWLock(app_info["lock_backend"])
    STATE_ONLINE = "online"
    STATE_OFFLINE = "offline"
    STATE_UNKNOWN = "unknown"

    @readlock(file_lock, logger)
    def get_sending_info(self):
        """ Returns sending info

        :returns: sending info
        :rtype: dict
        """
        result = {
            'firewall_status': {"state": SendingFiles.STATE_UNKNOWN, "last_check": 0},
            'ucollect_status': {"state": SendingFiles.STATE_UNKNOWN, "last_check": 0},
        }
        try:
            content = self._file_content(SendingFiles.FW_PATH)
            if re.search(r"turris firewall working: yes", content):
                result['firewall_status']["state"] = SendingFiles.STATE_ONLINE
            else:
                result['firewall_status']["state"] = SendingFiles.STATE_OFFLINE
            match = re.search(r"last working timestamp: ([0-9]+)", content)
            if match:
                result['firewall_status']["last_check"] = int(match.group(1))
        except IOError:
            # file doesn't probably exists yet
            logger.warning("Failed to read file '%s'." % SendingFiles.FW_PATH)

        try:
            content = self._file_content(SendingFiles.UC_PATH)
            match = re.search(r"^(\w+)\s+([0-9]+)$", content)
            if not match:
                logger.error("Wrong format of file '%s'." % SendingFiles.UC_PATH)
            else:
                if match.group(1) == "online":
                    result['ucollect_status']["state"] = SendingFiles.STATE_ONLINE
                else:
                    result['ucollect_status']["state"] = SendingFiles.STATE_OFFLINE
                result['ucollect_status']["last_check"] = int(match.group(2))

        except IOError:
            # file doesn't probably exists yet
            logger.warning("Failed to read file '%s'." % SendingFiles.UC_PATH)

        return result
