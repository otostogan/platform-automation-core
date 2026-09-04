import unittest

from platform_automation.operator.tailnet import find_peer, parse_status

STATUS = {
    "BackendState": "Running",
    "MagicDNSSuffix": "tailnet.example.net",
    "Self": {"DNSName": "laptop.tailnet.example.net.", "HostName": "laptop"},
    "Peer": {
        "n1": {
            "DNSName": "platform-host-1.tailnet.example.net.",
            "HostName": "platform-host-1",
            "Online": True,
            "Tags": ["tag:server-platform"],
        },
        "n2": {
            "DNSName": "platform-host-2.tailnet.example.net.",
            "HostName": "platform-host-2",
            "Online": False,
        },
    },
}


class ParseStatusTest(unittest.TestCase):
    def test_running_tailnet_with_peers(self) -> None:
        tailnet = parse_status(STATUS)

        self.assertTrue(tailnet.running)
        self.assertEqual(tailnet.self_dns, "laptop.tailnet.example.net")
        self.assertEqual(tailnet.suffix, "tailnet.example.net")
        self.assertEqual(len(tailnet.peers), 2)
        self.assertEqual(
            tailnet.peers["platform-host-1.tailnet.example.net"].tags,
            ("tag:server-platform",),
        )

    def test_stopped_backend_is_not_running(self) -> None:
        tailnet = parse_status({"BackendState": "Stopped", "Peer": None})

        self.assertTrue(tailnet.available)
        self.assertFalse(tailnet.running)
        self.assertEqual(tailnet.peers, {})

    def test_garbage_is_unavailable(self) -> None:
        self.assertFalse(parse_status(["not", "a", "status"]).available)


class FindPeerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tailnet = parse_status(STATUS)

    def test_magicdns_name_with_or_without_trailing_dot(self) -> None:
        for name in (
            "platform-host-1.tailnet.example.net",
            "platform-host-1.tailnet.example.net.",
            "Platform-Host-1.tailnet.example.net",
        ):
            peer = find_peer(self.tailnet, name)
            self.assertIsNotNone(peer, name)
            self.assertTrue(peer.online)

    def test_bare_host_name_resolves_through_the_suffix(self) -> None:
        peer = find_peer(self.tailnet, "platform-host-2")

        self.assertIsNotNone(peer)
        self.assertFalse(peer.online)

    def test_unknown_host_is_none(self) -> None:
        self.assertIsNone(find_peer(self.tailnet, "platform-host-9"))


if __name__ == "__main__":
    unittest.main()
