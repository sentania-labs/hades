# Talk to Hades

Open **Hades** in the Work navigation, or **Talk to Hades** from the board. The first
visit by an operator creates that operator's principal room with the configured room
harness and model. Its transcript belongs to Hades and survives runner restarts.

Messages are written before delivery and replies appear as they stream. **Interrupt**
is available only while an assistant turn is open and stops that reply. **Talking to**
switches the model while keeping the transcript and memory; the system line in the transcript records that switch. Decision records and
tool calls also appear inline. **History** loads older turns in bounded increments,
up to the latest 500 turns. At that limit the page shows a notice instead of another
History link; older turns remain stored.

If the runner cannot start after a message is saved, the composer clears that message
and says it is queued. Do not resend it: the transcript already contains it. A rejected
message remains in the composer so it can be corrected.

The connection label says **Cold** while there is no warm runner. Observer accounts can
read the room and its history, but cannot send, interrupt, or switch.

Each card also has its own [Thread panel](card-threads.md), using the same room controls
and stream. Card actions remain beside that panel in Actions.
