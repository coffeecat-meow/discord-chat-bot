from __future__ import annotations


async def run(executor, turn, name, a):
    import discord

    event = turn.event
    channel = executor.access.channel(turn, event.channel_id)
    if name == "TEXT_TRANSFORM":
        from .text_transform import transform_text

        return transform_text(a["operation"], a["text"])
    if name.startswith("MATH_"):
        from .math_tools import MathToolkit

        if name == "MATH_CALC":
            return await MathToolkit.calculate(a["expression"])
        buffer = await (
            MathToolkit.plot(a["expression"])
            if name == "MATH_PLOT"
            else MathToolkit.render_latex(a["expression"])
        )
        msg = await channel.send(file=discord.File(buffer, filename="math.png"))
        return {"url": msg.jump_url}
    raise ValueError(f"Unsupported tool: {name}")
