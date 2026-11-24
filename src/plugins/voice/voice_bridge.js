import http from "node:http";
import {
  EndBehaviorType,
  joinVoiceChannel,
  getVoiceConnection,
} from "@discordjs/voice";
import {
  Client,
  GatewayIntentBits,
  ChannelType,
} from "discord.js";
import prism from "prism-media";

const token = process.env.BOT_TOKEN;
const bridgePort = Number(process.env.VOICE_BRIDGE_PORT || "8787");
const pcmUrl = process.env.VOICE_BRIDGE_PCM_URL;
const bridgeSecret = process.env.VOICE_BRIDGE_SECRET;
const silenceSeconds = Number(process.env.VOICE_ASR_SILENCE_SECONDS || "1.5");

if (!token) {
  throw new Error("BOT_TOKEN is required");
}
if (!pcmUrl) {
  throw new Error("VOICE_BRIDGE_PCM_URL is required");
}
if (!bridgeSecret) {
  throw new Error("VOICE_BRIDGE_SECRET is required");
}

const client = new Client({
  intents: [
    GatewayIntentBits.Guilds,
    GatewayIntentBits.GuildVoiceStates,
  ],
});

const activeConnections = new Map();
let bridgeReady = false;
let bridgeServerStarted = false;

function readJson(req) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let length = 0;
    req.on("data", (chunk) => {
      length += chunk.length;
      if (length > 12 * 1024 * 1024) {
        reject(new Error("Request body too large"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => {
      try {
        const body = Buffer.concat(chunks).toString("utf8");
        resolve(body ? JSON.parse(body) : {});
      } catch (error) {
        reject(error);
      }
    });
  });
}

async function sendPcm(userId, guildId, channelId, pcm) {
  const response = await fetch(pcmUrl, {
    method: "POST",
    headers: {
      "content-type": "application/json",
      "x-voice-bridge-secret": bridgeSecret,
    },
    body: JSON.stringify({
      userId,
      guildId,
      channelId,
      pcmBase64: pcm.toString("base64"),
    }),
  });
  if (!response.ok) throw new Error(`PCM callback HTTP ${response.status}`);
}

async function joinVoice(guildId, channelId) {
  if (!bridgeReady) {
    throw new Error("Discord voice bridge is not ready");
  }
  // console.log(guildId, ", ", channelId);
  let normalizedGuildId = String(guildId);
  let guild = client.guilds.cache.get(normalizedGuildId);
  for (let attempt = 0; !guild && attempt < 20; attempt += 1) {
    await new Promise((resolve) => setTimeout(resolve, 250));
    guild = client.guilds.cache.get(normalizedGuildId);
  }
  if (!guild) {
    if (client.guilds.cache.size === 1) {
      const onlyGuildId = client.guilds.cache.firstKey();
      const onlyGuild = client.guilds.cache.first();
      if (onlyGuild) {
        console.warn(
          `Guild ID mismatch; using sole cached guild ${onlyGuildId} instead of ${guildId}`
        );
        normalizedGuildId = String(onlyGuildId);
        guild = onlyGuild;
      }
    }
  }
  if (!guild) {
    throw new Error(
      `Unknown Guild ${guildId}; cached guilds: ${Array.from(client.guilds.cache.keys()).join(",") || "none"}`
    );
  }

  const normalizedChannelId = String(channelId);
  let channel = guild.channels?.cache?.get(normalizedChannelId);
  if (!channel && guild.channels?.fetch) {
    try {
      channel = await guild.channels.fetch(normalizedChannelId);
    } catch (error) {
      const voiceChannelIds = guild.channels?.cache
        ? guild.channels.cache
            .filter((item) => item.type === ChannelType.GuildVoice)
            .map((item) => item.id)
            .join(",")
        : "";
      throw new Error(
        `Unknown Channel ${channelId}; guild=${normalizedGuildId}; voice channels=${voiceChannelIds || "none"}`
      );
    }
  }
  if (!channel || channel.type !== ChannelType.GuildVoice) {
    throw new Error(`Voice channel not found: ${channelId}; guild=${normalizedGuildId}`);
  }

  const previous = activeConnections.get(normalizedGuildId);
  if (previous?.joinConfig?.channelId === normalizedChannelId) {
    return { guildId: normalizedGuildId, channelId: normalizedChannelId, alreadyJoined: true };
  }
  if (previous) {
    previous.destroy();
    activeConnections.delete(normalizedGuildId);
  }

  const connection = joinVoiceChannel({
    channelId,
    guildId: normalizedGuildId,
    adapterCreator: guild.voiceAdapterCreator,
    selfDeaf: false,
  });
  
  const activeSubscriptions = new Set();
  const receiver = connection.receiver;
  receiver.speaking.on("start", (userId) => {
    // 1. 去重關鍵：如果該使用者已經在訂閱狀態中，直接忽略這次的 start 事件
    if (activeSubscriptions.has(userId)) {
      return;
    }

    activeSubscriptions.add(userId);

    const subscription = receiver.subscribe(userId, {
      end: {
        behavior: EndBehaviorType.AfterSilence,
        duration: Math.round(silenceSeconds * 1000),
      },
    });

    const decoder = new prism.opus.Decoder({
      frameSize: 960,
      channels: 2,
      rate: 48000,
    });

    const chunks = [];

    // 使用 pipe 自動處理背壓與串流傳遞
    subscription.pipe(decoder);

    decoder.on("data", (pcmChunk) => {
      chunks.push(pcmChunk);
    });

    // 統一的清理函式，確保無論成功或報錯都能釋放鎖定狀態
    const cleanup = () => {
      activeSubscriptions.delete(userId);
      if (!decoder.destroyed) decoder.destroy();
      if (!subscription.destroyed) subscription.destroy();
    };

    subscription.on("error", (error) => {
      console.error("Voice subscription error:", error);
      cleanup();
    });

    decoder.on("error", (error) => {
      console.error("Opus decoder error:", error);
      cleanup();
    });

    // 當靜音滿 1500ms，subscription 會自動結束並觸發 decoder 的 end 事件
    decoder.on("end", async () => {
      cleanup(); // 錄製結束，解除鎖定讓下次說話可以重新訂閱

      if (!chunks.length) return;

      try {
        await sendPcm(
          userId,
          normalizedGuildId,
          channelId,
          Buffer.concat(chunks),
        );
      } catch (error) {
        console.error("PCM callback failed:", error);
      }
    });
  });

  activeConnections.set(normalizedGuildId, connection);
  return { guildId: normalizedGuildId, channelId };
}

async function leaveVoice(guildId) {
  const connection =
    activeConnections.get(guildId) || getVoiceConnection(guildId);
  if (connection) {
    connection.destroy();
  }
  activeConnections.delete(guildId);
  return { guildId };
}

const server = http.createServer(async (req, res) => {
  res.setHeader("content-type", "application/json");
  try {
    if ((req.url === "/join" || req.url === "/leave") &&
        req.headers["x-voice-bridge-secret"] !== bridgeSecret) {
      res.statusCode = 401;
      res.end(JSON.stringify({ error: "unauthorized" }));
      return;
    }
    if (req.method === "GET" && req.url === "/health") {
      res.end(JSON.stringify({
        ready: bridgeReady,
        connected: activeConnections.size,
        guildIds: Array.from(client.guilds.cache.keys()),
      }));
      return;
    }
    if (req.method === "POST" && req.url === "/join") {
      const body = await readJson(req);
      console.log(body);
      res.end(JSON.stringify(await joinVoice(body.guildId, body.channelId)));
      return;
    }
    if (req.method === "POST" && req.url === "/leave") {
      const body = await readJson(req);
      res.end(JSON.stringify(await leaveVoice(body.guildId)));
      return;
    }
    res.statusCode = 404;
    res.end(JSON.stringify({ error: "not found" }));
  } catch (error) {
    res.statusCode = 500;
    res.end(JSON.stringify({ error: String(error?.message || error) }));
  }
});

function startBridgeServer() {
  if (bridgeServerStarted) return;
  bridgeServerStarted = true;
  bridgeReady = true;
  console.log(`Discord voice bridge ready as ${client.user?.tag}`);
  console.log(
    `Discord voice bridge guild cache: ${Array.from(client.guilds.cache.keys()).join(",") || "none"}`
  );
  server.listen(bridgePort, "127.0.0.1", () => {
    console.log(`Voice bridge listening on 127.0.0.1:${bridgePort}`);
  });
}

client.once("clientReady", startBridgeServer);

client.once("disconnect", () => {
  bridgeReady = false;
});

/*
  Legacy-compatible ready fallback for discord.js 14.25+.
*/
if (typeof client.login === "function") {
  await client.login(token);
}
