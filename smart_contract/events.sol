// SPDX-License-Identifier: MIT
pragma solidity 0.8.18;

contract Events {
    event LogMessage(address from, string event_type, string event_message);

    function emitEvent(string event_type, string event_message) public {
        emit LogMessage(msg.sender, event_type, event_message);
    }
}