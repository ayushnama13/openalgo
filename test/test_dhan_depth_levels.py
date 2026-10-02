import struct
from unittest.mock import MagicMock, patch

import pytest

import websocket_proxy  # noqa: F401
from broker.dhan.streaming.dhan_adapter import DhanWebSocketAdapter
from broker.dhan.streaming.dhan_mapping import DhanCapabilityRegistry, DhanExchangeMapper
from broker.dhan.streaming.dhan_websocket import DhanWebSocket


def test_dhan_websocket_urls():
    """Test URL construction for 5, 20, and 200 depth levels"""
    ws_5 = DhanWebSocket("client1", "token123", depth_level=5)
    assert "wss://api-feed.dhan.co" in ws_5.ws_url
    assert "version=2" in ws_5.ws_url

    ws_20 = DhanWebSocket("client1", "token123", depth_level=20)
    assert "wss://depth-api-feed.dhan.co/twentydepth" in ws_20.ws_url

    ws_200 = DhanWebSocket("client1", "token123", depth_level=200)
    assert "wss://full-depth-api.dhan.co/twohundreddepth" in ws_200.ws_url


def test_dhan_parse_depth_packets_200_and_7_rows():
    """Test parsing of 200-row bid packet and 7-row packet"""
    # 200-depth client
    ws_200 = DhanWebSocket("client1", "token123", depth_level=200)

    # Build 200-row bid packet (code 41)
    # Header: length (u16), code (u8)=41, segment (u8)=1, secid (u32)=1333, header_rows (u32)=200
    num_rows = 200
    payload_len = num_rows * 16
    msg_len = 12 + payload_len
    header = struct.pack("<HBBII", msg_len, 41, 1, 1333, num_rows)

    payload = bytearray()
    for i in range(num_rows):
        price = 1000.0 + i
        qty = 100 * (i + 1)
        orders = 2
        payload.extend(struct.pack("<dII", price, qty, orders))

    packet = header + bytes(payload)
    parsed = ws_200._parse_depth_packet(packet[12:], 1, 1333, is_bid=True, header_rows=200)

    assert parsed["type"] == "depth_200"
    assert parsed["side"] == "buy"
    assert len(parsed["levels"]) == 200
    assert parsed["levels"][0]["price"] == 1000.0
    assert parsed["levels"][199]["price"] == 1199.0

    # Build 7-row packet (e.g. header rows 7, payload has 7 rows)
    num_rows_7 = 7
    payload_len_7 = num_rows_7 * 16
    msg_len_7 = 12 + payload_len_7
    header_7 = struct.pack("<HBBII", msg_len_7, 41, 1, 1333, num_rows_7)
    payload_7 = bytearray()
    for i in range(num_rows_7):
        payload_7.extend(struct.pack("<dII", 500.0 + i, 50, 1))

    packet_7 = header_7 + bytes(payload_7)
    parsed_7 = ws_200._parse_depth_packet(packet_7[12:], 1, 1333, is_bid=True, header_rows=num_rows_7)

    assert parsed_7["type"] == "depth_200"
    assert len(parsed_7["levels"]) == 7


def test_dhan_capability_registry_values():
    """Test capability registry values for depth support and max subscriptions"""
    assert DhanCapabilityRegistry.is_depth_level_supported("NSE", 200) is True
    assert DhanCapabilityRegistry.is_depth_level_supported("NFO", 200) is True
    assert DhanCapabilityRegistry.is_depth_level_supported("BSE", 200) is False
    assert DhanCapabilityRegistry.is_depth_level_supported("BSE", 5) is True

    assert DhanCapabilityRegistry.get_supported_depth_levels("NSE") == {5, 20, 200}
    assert DhanCapabilityRegistry.get_supported_depth_levels("BSE") == {5}

    assert DhanCapabilityRegistry.get_max_subscriptions(5) == 5000
    assert DhanCapabilityRegistry.get_max_subscriptions(20) == 50
    assert DhanCapabilityRegistry.get_max_subscriptions(200) == 1

    # Fallback test: BSE requested 200 falls back to 5
    assert DhanCapabilityRegistry.get_fallback_depth_level("BSE", 200) == 5
    # NSE requested 200 stays 200
    assert DhanCapabilityRegistry.get_fallback_depth_level("NSE", 200) == 200


@patch("websocket_proxy.mapping.SymbolMapper.get_token_from_symbol")
@patch("broker.dhan.streaming.dhan_mapping.DhanExchangeMapper.get_dhan_exchange")
def test_dhan_adapter_subscribe_200_suffix(mock_get_dhan_exchange, mock_get_token):
    """Test adapter subscribe with :200 suffix picks 200 depth and enforces max 1 subscription"""
    mock_get_token.return_value = {"token": "1333", "brexchange": "NSE"}
    mock_get_dhan_exchange.return_value = "NSE_EQ"

    adapter = DhanWebSocketAdapter()
    adapter.ws_client_200depth = MagicMock()
    adapter.ws_client_200depth.connected = True

    # Subscribe first instrument with :200 suffix
    res1 = adapter.subscribe("RELIANCE:200", "NSE", mode=3)
    assert res1["status"] == "success"
    assert res1["actual_depth"] == 200
    assert any(s["actual_symbol"] == "RELIANCE" for s in adapter.subscriptions_200depth.values())

    # Try subscribing a second different symbol with :200 -> should return SUBSCRIPTION_LIMIT
    mock_get_token.return_value = {"token": "1334", "brexchange": "NSE"}
    res2 = adapter.subscribe("TCS:200", "NSE", mode=3)
    assert res2["status"] == "error"
    assert res2["code"] == "SUBSCRIPTION_LIMIT"
