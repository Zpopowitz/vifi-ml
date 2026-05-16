import { defineConfig } from "astro/config";
import tailwind from "@astrojs/tailwind";

export default defineConfig({
  site: "https://vifi.health",
  integrations: [tailwind()],
});
