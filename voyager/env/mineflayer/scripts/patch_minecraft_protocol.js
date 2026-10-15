// Fix minecraft-protocol's signed-chat verification (1.60.0): it spreads the
// previous-message list into Buffer.concat, which throws ERR_INVALID_ARG_TYPE
// and kills the bot process on the first signed player chat message.
// Run automatically after `npm install` (package.json postinstall).
const fs = require("fs");
const path = require("path");

const file = path.join(__dirname, "..", "node_modules", "minecraft-protocol", "src", "client", "chat.js");
if (!fs.existsSync(file)) {
    console.log("patch_minecraft_protocol: chat.js not found, nothing to do");
    process.exit(0);
}
const src = fs.readFileSync(file, "utf8");
const broken = "Buffer.concat(...previousMessages.map(msg => msg.signature || client._signatureCache[msg.id]))";
const fixed = "Buffer.concat(previousMessages.map(msg => msg.signature || client._signatureCache[msg.id]).filter(Boolean))";
if (src.includes(fixed)) {
    console.log("patch_minecraft_protocol: already patched");
} else if (src.includes(broken)) {
    fs.writeFileSync(file, src.replace(broken, fixed));
    console.log("patch_minecraft_protocol: patched chat.js");
} else {
    console.log("patch_minecraft_protocol: unexpected chat.js contents, not patched");
}
