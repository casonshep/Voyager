// Minecraft facts that are not in minecraft-data recipes: what blocks drop,
// what smelts into what, what burns, which mob drops which item, and how to
// read "a set of iron armor". Shared by the fast loop and the goal planner.

const ORE_DROPS = {
    coal_ore: "coal", deepslate_coal_ore: "coal",
    iron_ore: "raw_iron", deepslate_iron_ore: "raw_iron",
    copper_ore: "raw_copper", deepslate_copper_ore: "raw_copper",
    gold_ore: "raw_gold", deepslate_gold_ore: "raw_gold",
    diamond_ore: "diamond", deepslate_diamond_ore: "diamond",
    lapis_ore: "lapis_lazuli", deepslate_lapis_ore: "lapis_lazuli",
    redstone_ore: "redstone", deepslate_redstone_ore: "redstone",
    emerald_ore: "emerald", deepslate_emerald_ore: "emerald",
    stone: "cobblestone", deepslate: "cobbled_deepslate", grass_block: "dirt",
    gravel: "gravel", sand: "sand", dirt: "dirt", clay: "clay_ball",
    sugar_cane: "sugar_cane", bamboo: "bamboo", cactus: "cactus",
    pumpkin: "pumpkin", melon: "melon_slice", obsidian: "obsidian",
};

const SMELTABLE = {
    raw_iron: "iron_ingot", raw_copper: "copper_ingot", raw_gold: "gold_ingot",
    beef: "cooked_beef", porkchop: "cooked_porkchop", mutton: "cooked_mutton",
    chicken: "cooked_chicken", cod: "cooked_cod", salmon: "cooked_salmon",
    potato: "baked_potato", cobblestone: "stone", sand: "glass", clay_ball: "brick",
    oak_log: "charcoal", spruce_log: "charcoal", birch_log: "charcoal",
};

const FUELS = ["coal", "charcoal", "oak_planks", "spruce_planks", "birch_planks",
    "jungle_planks", "acacia_planks", "dark_oak_planks", "oak_log", "spruce_log",
    "birch_log", "jungle_log", "acacia_log", "dark_oak_log", "stick"];

const PICKAXE_TIER = ["wooden", "stone", "iron", "diamond", "netherite"];

const EDIBLE = [
    "cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken",
    "bread", "apple", "baked_potato", "cooked_cod", "cooked_salmon",
    "beef", "porkchop", "mutton", "chicken", "carrot", "potato", "melon_slice",
    "sweet_berries", "rotten_flesh",
];

// Blocks that occur naturally and are obtained by mining. Placeable crafted
// blocks (planks, crafting table, furnace) are NOT natural: they are crafted.
const NATURAL_BLOCK_RE =
    /_log$|_ore$|^stone$|^cobblestone$|^dirt$|^sand$|^red_sand$|^gravel$|^deepslate$|^grass_block$|_leaves$|^obsidian$|^clay$|^sugar_cane$|^bamboo$|^cactus$|^pumpkin$|^melon$|^sweet_berry_bush$|^wheat$|^netherrack$|^andesite$|^diorite$|^granite$|^tuff$|^calcite$|^snow_block$|^ice$|^sandstone$|^moss_block$|^kelp$|^seagrass$/;

// Named item families usable as goal targets: {item: "family:food", count: 8}
const FAMILIES = {
    food: EDIBLE,
    cooked_food: ["cooked_beef", "cooked_porkchop", "cooked_mutton", "cooked_chicken", "bread", "baked_potato", "cooked_cod", "cooked_salmon"],
    log: ["oak_log", "spruce_log", "birch_log", "jungle_log", "acacia_log", "dark_oak_log", "mangrove_log"],
    planks: ["oak_planks", "spruce_planks", "birch_planks", "jungle_planks", "acacia_planks", "dark_oak_planks", "mangrove_planks"],
    pickaxe: ["wooden_pickaxe", "stone_pickaxe", "iron_pickaxe", "diamond_pickaxe", "netherite_pickaxe"],
    bed: ["white_bed", "red_bed", "blue_bed", "black_bed", "gray_bed", "brown_bed", "green_bed", "yellow_bed", "orange_bed", "pink_bed", "purple_bed", "cyan_bed", "lime_bed", "magenta_bed", "light_blue_bed", "light_gray_bed"],
};

// item -> mob that drops it (for requirements no recipe or block can meet)
const MOB_DROPS = {
    leather: "cow", beef: "cow", porkchop: "pig", mutton: "sheep", white_wool: "sheep",
    chicken: "chicken", feather: "chicken", egg: "chicken",
    string: "spider", spider_eye: "spider", bone: "skeleton", arrow: "skeleton",
    gunpowder: "creeper", rotten_flesh: "zombie", ender_pearl: "enderman",
    blaze_rod: "blaze", slime_ball: "slime", phantom_membrane: "phantom",
    cod: "cod", salmon: "salmon", ink_sac: "squid",
};

// "a set of iron armor" -> four items; "a set of stone tools" -> three
const SET_EXPANSIONS = {
    armor: ["helmet", "chestplate", "leggings", "boots"],
    armour: ["helmet", "chestplate", "leggings", "boots"],
    gear: ["helmet", "chestplate", "leggings", "boots"],
    tools: ["pickaxe", "axe", "shovel"],
};
const SET_MATERIALS = ["wooden", "stone", "iron", "golden", "diamond", "netherite", "leather", "chainmail"];
const SYNONYMS = { pants: "leggings", helm: "helmet", chest_plate: "chestplate", gold: "golden", wood: "wooden" };

module.exports = {
    ORE_DROPS, SMELTABLE, FUELS, PICKAXE_TIER, EDIBLE, MOB_DROPS, NATURAL_BLOCK_RE, FAMILIES,
    SET_EXPANSIONS, SET_MATERIALS, SYNONYMS,
};
