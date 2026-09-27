// A steady 30 ms clock for voice detection. Browsers slow down timers on a hidden
// page (another tab, a covered window), but not in a worker, so listening keeps
// working while the page is out of sight.
"use strict";
setInterval(() => postMessage(0), 30);
