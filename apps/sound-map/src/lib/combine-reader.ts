import type { CombineMessage } from "./types.ts";

/** Consume NDJSON across arbitrary chunks, including a final line without a newline. */
export async function readComposition(response: Response, receive: (message: CombineMessage) => void): Promise<void> {
  if (!response.body) throw new Error("The composition service returned no progress stream.");
  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  const deliver = (line: string) => {
    if (!line.trim()) return false;
    const message = JSON.parse(line) as CombineMessage;
    receive(message);
    return message.type === "done" || message.type === "error";
  };
  try {
    for (;;) {
      const { value, done } = await reader.read();
      if (done) {
        if (deliver(buffer)) return;
        throw new Error("The connection ended before the piece was ready.");
      }
      buffer += value;
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) if (deliver(line)) return;
    }
  } finally {
    await reader.cancel().catch(() => {});
  }
}
