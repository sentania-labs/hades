# Talk to Hades

Open **Hades** in the Work navigation, or **Talk to Hades** from the board. The first
visit by an operator creates that operator's principal room with the configured room
harness and model. Its transcript belongs to Hades and survives runner restarts.

Messages are written before delivery and replies appear as they stream. **Interrupt**
stops the current reply. **Talking to** switches the model while keeping the transcript
and memory; the system line in the transcript records that switch. Decision records and
tool calls also appear inline. **History** loads older turns in bounded increments.

The connection label says **Cold** while there is no warm runner. Observer accounts can
read the room and its history, but cannot send, interrupt, or switch.
