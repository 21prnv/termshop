import { Container } from "@cloudflare/containers";

export class TermshopDemo extends Container {
  defaultPort = 8000;
  sleepAfter = "15m";
  envVars = { PUBLIC_URL: "https://termshop-demo.pranavpatil6251.workers.dev" };
}

export default {
  async fetch(request: Request, env: Env): Promise<Response> {
    // one shared demo instance; textual-serve spawns a process per visitor
    const container = env.TERMSHOP_DEMO.getByName("demo-v2");
    return container.fetch(request); // fetch() (not containerFetch) so websockets upgrade
  },
} satisfies ExportedHandler<Env>;

interface Env {
  TERMSHOP_DEMO: DurableObjectNamespace<TermshopDemo>;
}
