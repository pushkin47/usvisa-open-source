import asyncio
import unittest
from unittest.mock import patch

from test_support import TempProjectCaseMixin, reload_modules


class VPNConfigTests(TempProjectCaseMixin, unittest.TestCase):
    def setUp(self):
        super().setUp()
        self.vpn_module, = reload_modules("vpn_singbox_config")

    def test_build_config_filters_profiles_and_creates_selector(self):
        self.vpn_module.os.environ["USVISA_VPN_LOCATION_FILTER"] = "germany,poland"
        profiles = [
            {
                "remarks": "Germany Fast",
                "outbounds": [
                    {
                        "protocol": "vless",
                        "settings": {"vnext": [{"address": "de.example.test", "port": 443, "users": [{"id": "uuid-1"}]}]},
                        "streamSettings": {
                            "realitySettings": {"publicKey": "pk-1", "serverName": "de.example.test"},
                            "grpcSettings": {"serviceName": "svc"},
                        },
                    }
                ],
            },
            {
                "remarks": "Russia Blocked",
                "outbounds": [
                    {
                        "protocol": "vless",
                        "settings": {"vnext": [{"address": "ru.example.test", "port": 443, "users": [{"id": "uuid-2"}]}]},
                        "streamSettings": {
                            "realitySettings": {"publicKey": "pk-2", "serverName": "ru.example.test"},
                            "grpcSettings": {"serviceName": "svc"},
                        },
                    }
                ],
            },
        ]

        config = self.vpn_module.build_config(profiles)

        vless_outbounds = [item for item in config["outbounds"] if item["type"] == "vless"]
        self.assertEqual(len(vless_outbounds), 1)
        self.assertEqual(vless_outbounds[0]["server"], "de.example.test")
        self.assertEqual(config["route"]["final"], "auto")

    def test_build_config_creates_dedicated_proxy_ports_for_different_outbounds(self):
        profiles = [
            {
                "remarks": "Germany Fast",
                "outbounds": [
                    {
                        "protocol": "vless",
                        "settings": {"vnext": [{"address": "de.example.test", "port": 443, "users": [{"id": "uuid-1"}]}]},
                        "streamSettings": {
                            "realitySettings": {"publicKey": "pk-1", "serverName": "de.example.test"},
                            "grpcSettings": {"serviceName": "svc"},
                        },
                    }
                ],
            },
            {
                "remarks": "Poland Fast",
                "outbounds": [
                    {
                        "protocol": "vless",
                        "settings": {"vnext": [{"address": "pl.example.test", "port": 443, "users": [{"id": "uuid-2"}]}]},
                        "streamSettings": {
                            "realitySettings": {"publicKey": "pk-2", "serverName": "pl.example.test"},
                            "grpcSettings": {"serviceName": "svc"},
                        },
                    }
                ],
            },
        ]

        config = self.vpn_module.build_config(profiles)

        self.assertEqual([item["listen_port"] for item in config["inbounds"]], [2080, 2081])
        self.assertEqual(
            config["route"]["rules"],
            [
                {"inbound": ["mixed-in-1"], "outbound": "proxy-1-germany-fast"},
                {"inbound": ["mixed-in-2"], "outbound": "proxy-2-poland-fast"},
            ],
        )

    def test_build_config_falls_back_when_include_filter_matches_nothing(self):
        self.vpn_module.os.environ["USVISA_VPN_LOCATION_FILTER"] = "nowhere"
        profile = {
            "remarks": "Poland Node",
            "outbounds": [
                {
                    "protocol": "vless",
                    "settings": {"vnext": [{"address": "pl.example.test", "port": 443, "users": [{"id": "uuid-3"}]}]},
                    "streamSettings": {
                        "realitySettings": {"publicKey": "pk-3", "serverName": "pl.example.test"},
                        "grpcSettings": {"serviceName": "svc"},
                    },
                }
            ],
        }

        config = self.vpn_module.build_config([profile])

        self.assertEqual(self.vpn_module.os.environ["USVISA_VPN_LOCATION_FILTER"], "")
        self.assertEqual([item for item in config["outbounds"] if item["type"] == "vless"][0]["server"], "pl.example.test")

    def test_build_config_raises_without_usable_profiles(self):
        with self.assertRaisesRegex(RuntimeError, "No usable VLESS Reality nodes"):
            self.vpn_module.build_config([{"remarks": "broken", "outbounds": []}])

    def test_safe_tag_and_truthy_helpers(self):
        self.assertEqual(self.vpn_module._safe_tag(" Germany #1 ", 2), "proxy-2-germany-1")
        self.assertTrue(self.vpn_module._truthy("YES"))
        self.assertFalse(self.vpn_module._truthy("0"))


class LocalEventTests(unittest.IsolatedAsyncioTestCase):
    async def test_event_is_only_written_to_console(self):
        from local_events import record_event
        with patch("builtins.print") as output:
            self.assertTrue(await record_event("synthetic event"))
        output.assert_called_once_with("synthetic event")


if __name__ == "__main__":
    unittest.main()
