# Brief — rundesk-skills-integrations

*What this catalog is and why it exists. One screen, and it changes when the catalog does.*

## Story

`rundesk-skills-integrations` gives a Rundesk agent guarded access to the services a team already
runs — Cloudflare, Atlassian, Coolify, Discord, Grafana, Monarch Money, PostHog, Sentry, Slack, and
Stripe. Each package ships one command, the guidance for using it, its credential declaration, and
offline tests.

One package, one service, one credential. Nothing here is a general-purpose HTTP client.

## Why it exists

An agent asked about an incident, a deploy, or a payment needs the service that holds the answer.
Letting it make arbitrary network calls would put every service behind one unbounded capability with
no way to say which agent may reach what.

A package per service makes the grant the boundary: the owner grants Sentry to one agent and Stripe
to none, and each package declares exactly the credential it needs.

## Users

- Rundesk agents that need first-hand data from a service rather than a recollection of it.
- The owner, granting one service at a time and able to see which agent holds which.

*Sourced from the readme, the environments contract, and the package contract.*

## Scope

- **Covers:** one guarded command per service, its operating guidance, its declared credential, and
  its offline tests, under the runtime and configuration contract in `ENVIRONMENTS.md`.
- **Refuses:**
  - A general HTTP or shell capability. Every network call goes through a package that declares what
    it reaches.
  - Holding a credential in the repository. A package declares a name; the value lives in Rundesk's
    own store.
  - Writing where reading answers the question. A package that can change a live service says so and
    guards it.
  - General method the default catalog owns, and Apple or Google services that have their own
    catalogs.

## External systems

- The integrated services themselves, each reached by one package: Cloudflare, Atlassian, Coolify,
  Discord, Grafana, Monarch Money, PostHog, Sentry, Slack, and Stripe.
- Rundesk — installs this catalog, holds the declared credentials, and grants packages per agent.
- GitHub — hosts the repository and serves the release a catalog install fetches.
