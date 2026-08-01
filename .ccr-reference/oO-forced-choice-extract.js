// LIVE ai-gateway bundle extracts (patched web-tools state) for the oO forced-choice fix
// oO = openai_responses tool_choice builder (UNPATCHED - the bug). Mirror the rO fix onto it.

function oO(e,n){if(e===void 0)return;if(typeof e=="string")return e;if(!f(e))return;let t=l(e.type);if(t==="auto"||t==="none")return t;if(t==="any"||t==="required")return"required";let r=vd(e),o=r?$t(r,n):void 0,i=o?.name;return i?{type:"function",name:i,...o?.namespace?{namespace:o.namespace}:{}}:e}

// rO = chat-completions tool_choice builder (ALREADY PATCHED - reference pattern):
function rO(e,n){if(e===void 0)return;if(typeof e=="string")return e;if(!f(e))return;let t=l(e.type);if(t==="auto"||t==="none")return t;if(t==="any"||t==="required")return"required";let r=vd(e);if(r&&(r==="web_search"||r==="web_fetch")&&Array.isArray(n)&&n.some(o=>qs(o)&&(l(o.name)||(ccrPolyphonyHostedWebFetchTool(o)?"web_fetch":"web_search"))===r))return{type:"web_search"};let o=Ft(e,n);return o?{type:"function",function:{name:o}}:e}

// helpers:
function vd(e){if(!f(e))return;let n=f(e.function)?e.function:void 0;return Cd(l(e.name)||l(n?.name),l(e.namespace)||l(n?.namespace))}
function Ft(e,n){let t=vd(e);return t?ot(t,n):void 0}
function qs(e){return Ht(e)||Kt(e)||ccrPolyphonyHostedWebFetchTool(e)}
function Ht(e){if(!f(e))return!1;let n=l(e.type);return n==="web_search"||n==="web_search_preview"}
function Kt(e){if(!f(e))return!1;let n=l(e.type);return!!(n&&/^web_search_\d{8}$/.test(n))}
function ccrPolyphonyHostedWebFetchTool(e){if(!f(e))return!1;let n=l(e.type);return n==="web_fetch"||n==="web_fetch_20260209"||n==="web_fetch_20250910"}
function ZM(e){let n={type:"web_search"},t=eO(e);return t&&(n.filters=t),n}

// oO call site: let r=oO(e.tool_choice,e.tools) (note: e.tool_choice/e.tools, NOT e.standardRequest.*)