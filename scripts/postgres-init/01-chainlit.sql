CREATE DATABASE chainlit;

\connect chainlit

CREATE EXTENSION IF NOT EXISTS pgcrypto;

CREATE TABLE IF NOT EXISTS "User" (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    identifier text NOT NULL UNIQUE,
    metadata text DEFAULT '{}'::text NOT NULL,
    "createdAt" timestamptz DEFAULT now() NOT NULL,
    "updatedAt" timestamptz DEFAULT now() NOT NULL
);

CREATE TABLE IF NOT EXISTS "Thread" (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    name text,
    "userId" uuid REFERENCES "User"(id) ON DELETE SET NULL,
    tags text[],
    metadata text DEFAULT '{}'::text NOT NULL,
    "createdAt" timestamptz DEFAULT now() NOT NULL,
    "deletedAt" timestamptz
);

CREATE TABLE IF NOT EXISTS "Step" (
    id uuid DEFAULT gen_random_uuid() PRIMARY KEY,
    "threadId" uuid REFERENCES "Thread"(id) ON DELETE CASCADE,
    "parentId" uuid REFERENCES "Step"(id) ON DELETE SET NULL,
    input jsonb DEFAULT '{}'::jsonb,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    name text,
    output jsonb DEFAULT '{}'::jsonb NOT NULL,
    type text NOT NULL,
    "startTime" timestamptz,
    "endTime" timestamptz,
    "showInput" text,
    "isError" boolean,
    "createdAt" timestamptz DEFAULT now()
);

CREATE TABLE IF NOT EXISTS "Feedback" (
    id text PRIMARY KEY,
    "stepId" uuid NOT NULL REFERENCES "Step"(id) ON DELETE CASCADE,
    name text DEFAULT 'user_feedback'::text NOT NULL,
    value double precision NOT NULL,
    comment text
);

CREATE TABLE IF NOT EXISTS "Element" (
    id text PRIMARY KEY,
    "threadId" uuid REFERENCES "Thread"(id) ON DELETE CASCADE,
    "stepId" uuid REFERENCES "Step"(id) ON DELETE CASCADE,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    mime text,
    name text NOT NULL,
    "objectKey" text,
    url text,
    "chainlitKey" text,
    display text,
    size text,
    language text,
    page integer,
    props jsonb DEFAULT '{}'::jsonb NOT NULL
);
