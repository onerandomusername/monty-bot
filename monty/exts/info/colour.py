import colorsys
import json
import pathlib
import random
import string
from io import BytesIO
from typing import Optional, Union

import disnake
import rapidfuzz
from disnake.ext import commands
from PIL import Image, ImageColor

from monty.bot import Monty
from monty.utils.extensions import invoke_help_command
from monty.utils.messages import DeleteButton


THUMBNAIL_SIZE = (80, 80)


class Colour(commands.Cog, slash_command_attrs={"dm_permission": False}):
    """Cog for the Colour command."""

    def __init__(self, bot: Monty) -> None:
        self.bot = bot
        with open(pathlib.Path("monty/resources/ryanzec_colours.json")) as f:
            self.colour_mapping = json.load(f)
            del self.colour_mapping["_"]  # Delete source credit entry

    async def send_colour_response(
        self, ctx: Union[commands.Context, disnake.CommandInteraction], rgb: tuple[int, int, int], input_colour: str
    ) -> None:
        """Create and send embed from user given colour information."""
        name = self._rgb_to_name(rgb)
        try:
            colour_or_color = ctx.invoked_parents[0]
        except (IndexError, AttributeError):
            if isinstance(ctx, disnake.Interaction):
                colour_or_color = "color" if ctx.locale is disnake.Locale.en_US else "colour"
            else:
                colour_or_color = "colour"

        if isinstance(ctx, disnake.CommandInteraction):
            colour_mode = ctx.application_command.name
            kwargs = ctx.filled_options
        else:
            colour_mode = ctx.invoked_with
            kwargs = ctx.kwargs

        if colour_mode == "random":
            colour_mode = colour_or_color
            input_colour = name
        elif colour_mode in ("colour", "color"):
            input_colour = kwargs["colour_input"]
        elif colour_mode == "name":
            input_colour = kwargs["name"]
        elif colour_mode == "hex":
            if len(input_colour) > 7:
                input_colour = input_colour[:-2]
        else:
            input_colour = str(rgb)

        if colour_mode in ("name", "hex", "random", "color", "colour"):
            colour_mode = colour_mode.title()
        else:
            colour_mode = colour_mode.upper()

        colour_embed = disnake.Embed(
            title=f"{name or input_colour}",
            description=f"{colour_or_color.title()} information for {colour_mode} `{input_colour or name}`.",
            colour=disnake.Color.from_rgb(*rgb),
        )
        colour_conversions = self.get_colour_conversions(rgb)
        for colour_space, value in colour_conversions.items():
            colour_embed.add_field(name=colour_space, value=f"`{value}`", inline=True)

        thumbnail = Image.new("RGB", THUMBNAIL_SIZE, color=rgb)
        buffer = BytesIO()
        thumbnail.save(buffer, "PNG")
        buffer.seek(0)
        thumbnail_file = disnake.File(buffer, filename="colour.png")

        colour_embed.set_thumbnail(url="attachment://colour.png")

        if isinstance(ctx, commands.Context):
            components = DeleteButton(ctx.author, initial_message=ctx.message)
        else:
            components = DeleteButton(ctx.author)
        await ctx.send(file=thumbnail_file, embed=colour_embed, components=components)

    @commands.group(aliases=("color",), invoke_without_command=True)
    async def colour(self, ctx: commands.Context, *, colour_input: Optional[str] = None) -> None:
        """
        Create an embed that displays colour information.

        If no subcommand is called, a randomly selected colour will be shown.
        """
        if colour_input is None:
            await self.random(ctx)
            return

        try:
            extra_colour = ImageColor.getrgb(colour_input)
            await self.send_colour_response(ctx, extra_colour, input_colour=colour_input)
        except ValueError:
            await invoke_help_command(ctx)

    @commands.slash_command(name=disnake.Localised("colour", data={disnake.Locale.en_US: "color"}))
    async def slash_colour(self, inter: disnake.CommandInteraction) -> None:
        """Show information about a colour."""
        pass

    @colour.command()
    async def rgb(self, ctx: commands.Context, red: int, green: int, blue: int) -> None:
        """Create an embed from an RGB input."""
        if any(c not in range(256) for c in (red, green, blue)):
            raise commands.BadArgument(
                message=f"RGB values can only be from 0 to 255. User input was: `{red, green, blue}`."
            )
        rgb_tuple = (red, green, blue)
        await self.send_colour_response(ctx, rgb_tuple, input_colour=f"{red}, {green}, {blue}")

    @slash_colour.sub_command(name="rgb")
    async def slash_rgb(
        self,
        inter: disnake.CommandInteraction,
        red: commands.Range[int, 0, 255],
        green: commands.Range[int, 0, 255],
        blue: commands.Range[int, 0, 255],
    ) -> None:
        """
        RGB Format.

        Parameters
        ----------
        red: Red.
        green: Green.
        blue: Blue.
        """
        rgb_tuple = (red, green, blue)
        await self.send_colour_response(inter, rgb_tuple, input_colour=f"{red}, {green}, {blue}")

    @colour.command()
    async def hsv(self, ctx: commands.Context, hue: int, saturation: int, value: int) -> None:
        """Create an embed from an HSV input."""
        if (hue not in range(361)) or any(c not in range(101) for c in (saturation, value)):
            raise commands.BadArgument(
                message=(
                    "Hue can only be from 0 to 360. Saturation and Value can only be from 0 to 100. "
                    f"User input was: `{hue, saturation, value}`."
                )
            )
        input_colour = f"hsv({hue}, {saturation}%, {value}%)"
        hsv_tuple = ImageColor.getrgb(input_colour)
        await self.send_colour_response(ctx, hsv_tuple, input_colour=input_colour)

    @slash_colour.sub_command(name="hsv")
    async def slash_hsv(
        self,
        inter: disnake.CommandInteraction,
        hue: commands.Range[int, 0, 360],
        sat: commands.Range[int, 0, 360],
        value: commands.Range[int, 0, 100],
    ) -> None:
        """
        HSV Format.

        Parameters
        ----------
        hue: Hue.
        sat: Saturation.
        value: Value.
        """
        input_colour = f"hsv({hue}, {sat}%, {value}%)"
        hsv_tuple = ImageColor.getrgb(input_colour)
        await self.send_colour_response(inter, hsv_tuple, input_colour=input_colour)

    @colour.command()
    async def hsl(self, ctx: commands.Context, hue: int, saturation: int, lightness: int) -> None:
        """Create an embed from an HSL input."""
        if (hue not in range(361)) or any(c not in range(101) for c in (saturation, lightness)):
            raise commands.BadArgument(
                message=(
                    "Hue can only be from 0 to 360. Saturation and Lightness can only be from 0 to 100. "
                    f"User input was: `{hue, saturation, lightness}`."
                )
            )
        input_colour = f"hsl({hue}, {saturation}%, {lightness}%)"
        hsl_tuple = ImageColor.getrgb(input_colour)
        await self.send_colour_response(ctx, hsl_tuple, input_colour=input_colour)

    @slash_colour.sub_command(name="hsl")
    async def slash_hsl(
        self,
        inter: disnake.CommandInteraction,
        hue: commands.Range[int, 0, 360],
        sat: commands.Range[int, 0, 360],
        lightness: commands.Range[int, 0, 100],
    ) -> None:
        """
        HSL Format.

        Parameters
        ----------
        hue: Hue.
        sat: Saturation.
        lightness: Lightness.
        """
        input_colour = f"hsl({hue}, {sat}%, {lightness}%)"
        hsl_tuple = ImageColor.getrgb(input_colour)
        await self.send_colour_response(inter, hsl_tuple, input_colour=input_colour)

    @colour.command()
    async def cmyk(self, ctx: commands.Context, cyan: int, magenta: int, yellow: int, key: int) -> None:
        """Create an embed from a CMYK input."""
        if any(c not in range(101) for c in (cyan, magenta, yellow, key)):
            raise commands.BadArgument(
                message=f"CMYK values can only be from 0 to 100. User input was: `{cyan, magenta, yellow, key}`."
            )
        r = round(255 * (1 - (cyan / 100)) * (1 - (key / 100)))
        g = round(255 * (1 - (magenta / 100)) * (1 - (key / 100)))
        b = round(255 * (1 - (yellow / 100)) * (1 - (key / 100)))
        await self.send_colour_response(ctx, (r, g, b), input_colour=f"CMYK: {cyan}, {magenta}, {yellow}, {key}")

    @slash_colour.sub_command(name="cymk")
    async def slash_cymk(
        self,
        inter: disnake.CommandInteraction,
        cyan: commands.Range[int, 0, 100],
        magenta: commands.Range[int, 0, 100],
        yellow: commands.Range[int, 0, 100],
        black: commands.Range[int, 0, 100],
    ) -> None:
        """
        CMYK Format.

        Parameters
        ----------
        cyan: Cyan.
        magenta: Magenta.
        yellow: Yellow.
        black: Black.
        """
        await self.cmyk(inter, cyan, magenta, yellow, black)

    @colour.command()
    async def hex(self, ctx: commands.Context, hex_code: str) -> None:
        """Create an embed from a HEX input."""
        if hex_code[0] != "#":
            hex_code = f"#{hex_code}"

        if len(hex_code) not in (4, 5, 7, 9) or any(digit not in string.hexdigits for digit in hex_code[1:]):
            raise commands.BadArgument(
                message=(
                    f"Cannot convert `{hex_code}` to a recognizable Hex format. "
                    "Hex values must be hexadecimal and take the form *#RRGGBB* or *#RGB*."
                )
            )

        hex_tuple = ImageColor.getrgb(hex_code)
        if len(hex_tuple) == 4:
            hex_tuple = hex_tuple[:-1]  # Colour must be RGB. If RGBA, we remove the alpha value
        await self.send_colour_response(ctx, hex_tuple, input_colour=hex_code)

    @slash_colour.sub_command(name="hex")
    async def slash_hex(self, inter: disnake.CommandInteraction, hex: str) -> None:
        """
        HEX Format.

        Parameters
        ----------
        hex: Hex colour code.
        """
        try:
            await self.hex(inter, hex)
        except commands.BadArgument as e:
            await inter.send(str(e), ephemeral=True)

    @colour.command()
    async def name(self, ctx: commands.Context, *, name: str) -> None:
        """Create an embed from a name input."""
        hex_colour = self.match_colour_name(ctx, name)
        if hex_colour is None:
            name_error_embed = disnake.Embed(
                title="No colour match found.",
                description=f"No colour found for: `{name}`",
                colour=disnake.Color.dark_red(),
            )
            await ctx.send(embed=name_error_embed)
            return
        hex_tuple = ImageColor.getrgb(hex_colour)
        await self.send_colour_response(ctx, hex_tuple, input_colour=name)

    @slash_colour.sub_command(name="name")
    async def slash_name(self, inter: disnake.CommandInteraction, name: str) -> None:
        """
        Get a colour by name.

        Parameters
        ----------
        name: Colour name, by close match.
        """
        await self.name(inter, name=name)

    @colour.command()
    async def random(self, ctx: commands.Context) -> None:
        """Create an embed from a randomly chosen colour."""
        hex_colour = random.choice(list(self.colour_mapping.values()))
        hex_tuple = ImageColor.getrgb(f"#{hex_colour}")
        await self.send_colour_response(ctx, hex_tuple, input_colour=None)

    @slash_colour.sub_command(name="random")
    async def slash_random(self, inter: disnake.CommandInteraction) -> None:
        """Random colour."""
        await self.random(inter)

    def get_colour_conversions(self, rgb: tuple[int, int, int]) -> dict[str, str]:
        """Create a dictionary mapping of colour types and their values."""
        colour_name = self._rgb_to_name(rgb)
        if colour_name is None:
            colour_name = "No match found"
        return {
            "RGB": rgb,
            "HSV": self._rgb_to_hsv(rgb),
            "HSL": self._rgb_to_hsl(rgb),
            "CMYK": self._rgb_to_cmyk(rgb),
            "Hex": self._rgb_to_hex(rgb),
            "Name": colour_name,
        }

    @staticmethod
    def _rgb_to_hsv(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
        """Convert RGB values to HSV values."""
        rgb_list = [val / 255 for val in rgb]
        h, s, v = colorsys.rgb_to_hsv(*rgb_list)
        hsv = (round(h * 360), round(s * 100), round(v * 100))
        return hsv

    @staticmethod
    def _rgb_to_hsl(rgb: tuple[int, int, int]) -> tuple[int, int, int]:
        """Convert RGB values to HSL values."""
        rgb_list = [val / 255.0 for val in rgb]
        h, l, s = colorsys.rgb_to_hls(*rgb_list)  # noqa: E741
        hsl = (round(h * 360), round(s * 100), round(l * 100))
        return hsl

    @staticmethod
    def _rgb_to_cmyk(rgb: tuple[int, int, int]) -> tuple[int, int, int, int]:
        """Convert RGB values to CMYK values."""
        rgb_list = [val / 255.0 for val in rgb]
        if not any(rgb_list):
            return 0, 0, 0, 100
        k = 1 - max(rgb_list)
        c = round((1 - rgb_list[0] - k) * 100 / (1 - k))
        m = round((1 - rgb_list[1] - k) * 100 / (1 - k))
        y = round((1 - rgb_list[2] - k) * 100 / (1 - k))
        cmyk = (c, m, y, round(k * 100))
        return cmyk

    @staticmethod
    def _rgb_to_hex(rgb: tuple[int, int, int]) -> str:
        """Convert RGB values to HEX code."""
        hex_ = "".join([hex(val)[2:].zfill(2) for val in rgb])
        hex_code = f"#{hex_}".upper()
        return hex_code

    def _rgb_to_name(self, rgb: tuple[int, int, int]) -> Optional[str]:
        """Convert RGB values to a fuzzy matched name."""
        input_hex_colour = self._rgb_to_hex(rgb)
        try:
            match, certainty, _ = rapidfuzz.process.extractOne(
                query=input_hex_colour, choices=self.colour_mapping.values(), score_cutoff=80
            )
            colour_name = [name for name, hex_code in self.colour_mapping.items() if hex_code == match][0]
        except TypeError:
            colour_name = None
        return colour_name

    def match_colour_name(self, ctx: commands.Context, input_colour_name: str) -> Optional[str]:
        """Convert a colour name to HEX code."""
        try:
            match, certainty, _ = rapidfuzz.process.extractOne(
                query=input_colour_name, choices=self.colour_mapping.keys(), score_cutoff=80
            )
        except (ValueError, TypeError):
            return
        return f"#{self.colour_mapping[match]}"


def setup(bot: Monty) -> None:
    """Load the Colour cog."""
    bot.add_cog(Colour(bot))
