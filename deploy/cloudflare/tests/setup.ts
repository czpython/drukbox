import { timingSafeEqual } from "node:crypto";

Object.assign(crypto.subtle, {
  timingSafeEqual: (a: ArrayBufferView, b: ArrayBufferView) =>
    timingSafeEqual(new Uint8Array(a.buffer, a.byteOffset, a.byteLength), new Uint8Array(b.buffer, b.byteOffset, b.byteLength)),
});
