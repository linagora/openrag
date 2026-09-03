import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { getVersion } from "@/lib/api/system";
import { hasNewIn, isNew } from "@/lib/whats-new";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { NewBadge, NewDot, useIsNew, useNewOptions } from "./new-badge";

// The registry itself is covered by lib/whats-new.test.ts. These tests are
// about attachment: that any surface gets the marker from a feature key alone,
// that the key it asks about is the one the surface declares, and that a marker
// dropped into a select does not surface twice.
vi.mock("@/lib/whats-new", () => ({ isNew: vi.fn(), hasNewIn: vi.fn() }));
vi.mock("@/lib/api/system", () => ({ getVersion: vi.fn() }));

const isNewMock = vi.mocked(isNew);
const hasNewInMock = vi.mocked(hasNewIn);
const getVersionMock = vi.mocked(getVersion);

/** Render under a fresh QueryClient, since every test drives `/version` itself. */
function renderWithQuery(ui: React.ReactNode) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(<QueryClientProvider client={queryClient}>{ui}</QueryClientProvider>);
}

beforeAll(() => {
  // Radix needs these to open a select under jsdom.
  if (!Element.prototype.hasPointerCapture) Element.prototype.hasPointerCapture = () => false;
  if (!Element.prototype.setPointerCapture) Element.prototype.setPointerCapture = () => {};
  if (!Element.prototype.releasePointerCapture) Element.prototype.releasePointerCapture = () => {};
  if (!Element.prototype.scrollIntoView) Element.prototype.scrollIntoView = () => {};
});

beforeEach(() => {
  vi.clearAllMocks();
  getVersionMock.mockResolvedValue({ version: "2.2.0" });
});

describe("NewBadge", () => {
  it("marks a feature that is still new, whatever surface it is dropped on", async () => {
    isNewMock.mockReturnValue(true);
    renderWithQuery(<NewBadge feature="nav.workspaces" />);

    expect(await screen.findByText("NEW")).not.toBeNull();
    await waitFor(() => expect(isNewMock).toHaveBeenCalledWith("nav.workspaces", "2.2.0"));
  });

  it("renders nothing once the feature has expired", async () => {
    isNewMock.mockReturnValue(false);
    renderWithQuery(<NewBadge feature="nav.workspaces" />);

    await waitFor(() => expect(getVersionMock).toHaveBeenCalled());
    expect(screen.queryByText("NEW")).toBeNull();
  });

  it("renders nothing while the app version is unknown", () => {
    // The version query has not resolved (and may never, on a deployment whose
    // /version cannot be read): the rule fails closed on `undefined`.
    isNewMock.mockImplementation((_feature, version) => version === "2.2.0");
    renderWithQuery(<NewBadge feature="nav.workspaces" />);

    expect(screen.queryByText("NEW")).toBeNull();
  });
});

describe("NewDot", () => {
  // The documented pattern for a dot outside a dropdown: the caller gates it on
  // the key, so the dot itself is presentational.
  /**
   * A surface outside any dropdown, written the way FEATURE_TAG.md documents it:
   * the caller gates on the key, so the dot itself stays presentational.
   */
  function NavEntry() {
    return <>{useIsNew("nav.workspaces") && <NewDot label="New page" />}</>;
  }

  it("renders under a caller-supplied name", () => {
    isNewMock.mockReturnValue(true);
    renderWithQuery(<NavEntry />);

    const dot = screen.getByRole("img", { name: "New page" });
    expect(dot).not.toBeNull();
    // The dot is sized, not filled, so it needs a box: width and height do not
    // apply to a non-replaced inline element, and this surface is not a flex
    // row. jsdom does no layout, so the class carrying that is what is
    // observable here.
    expect(dot.className).toContain("inline-block");
  });

  it("is absent when the feature is not new", () => {
    isNewMock.mockReturnValue(false);
    renderWithQuery(<NavEntry />);

    expect(screen.queryByRole("img", { name: "New page" })).toBeNull();
  });
});

describe("useNewOptions", () => {
  /** A group of options rendered with both markers the hook hands back. */
  function Options({ values }: { values: string[] }) {
    const options = useNewOptions("chunking", values);
    return (
      <div>
        <span data-testid="group">
          group{options.dot}
          {options.badge}
        </span>
        {values.map((value) => (
          <span key={value} data-testid={`option-${value}`}>
            {value}
            {options.badgeFor(value)}
          </span>
        ))}
      </div>
    );
  }

  it("marks the collapsed control when any option inside is new, and only the new option", async () => {
    // Version-sensitive, so the assertions below only pass once the version the
    // query fetched has actually reached the rule.
    hasNewInMock.mockImplementation((_group, _values, version) => version === "2.2.0");
    isNewMock.mockImplementation(
      (feature, version) => version === "2.2.0" && feature === "chunking.structured_section"
    );
    renderWithQuery(<Options values={["recursive_splitter", "structured_section"]} />);

    await waitFor(() => expect(screen.getByTestId("group").textContent).toBe("groupNEW"));
    // The quiet marker for a collapsed control, and it says what it means to a
    // reader who cannot see that it is a dot.
    expect(screen.getByRole("img", { name: /new options available/i })).not.toBeNull();
    expect(screen.getByTestId("option-structured_section").textContent).toBe("structured_sectionNEW");
    expect(screen.getByTestId("option-recursive_splitter").textContent).toBe("recursive_splitter");
    // The key each option asks about is derived from the group named once.
    expect(hasNewInMock).toHaveBeenCalledWith(
      "chunking",
      ["recursive_splitter", "structured_section"],
      "2.2.0"
    );
    expect(isNewMock).toHaveBeenCalledWith("chunking.structured_section", "2.2.0");
  });

  it("leaves the control unmarked when nothing inside is new", async () => {
    hasNewInMock.mockReturnValue(false);
    isNewMock.mockReturnValue(false);
    renderWithQuery(<Options values={["recursive_splitter"]} />);

    await waitFor(() => expect(hasNewInMock).toHaveBeenCalledWith("chunking", ["recursive_splitter"], "2.2.0"));
    expect(screen.getByTestId("group").textContent).toBe("group");
    expect(screen.queryByRole("img", { name: /new options available/i })).toBeNull();
  });
});

describe("the documented dropdown example (FEATURE_TAG.md)", () => {
  /**
   * The snippet the guide tells people to copy, rendered as written: the group
   * named once, the collapsed control carrying the marker for what is inside,
   * `textValue` keeping the marker out of Radix's typeahead text, and the marker
   * itself as a plain child of the item.
   */
  function StrategyField({ values }: { values: string[] }) {
    const newChunking = useNewOptions("chunking", values);
    return (
      <>
        <label htmlFor="strategy" data-testid="label">
          Strategy
          {newChunking.dot}
        </label>
        <Select defaultValue="structured_section">
          <SelectTrigger id="strategy" aria-label="Strategy">
            <SelectValue />
          </SelectTrigger>
          <SelectContent>
            {values.map((s) => (
              <SelectItem key={s} value={s} textValue={s}>
                <span>
                  {s}
                  {newChunking.badgeFor(s)}
                </span>
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </>
    );
  }

  beforeEach(() => {
    hasNewInMock.mockImplementation((_group, _values, version) => version === "2.2.0");
    isNewMock.mockImplementation(
      (feature, version) => version === "2.2.0" && feature === "chunking.structured_section"
    );
  });

  it("marks the collapsed control and the option, and hides the copy Radix mirrors into the trigger", async () => {
    renderWithQuery(<StrategyField values={["recursive_splitter", "structured_section"]} />);

    // The label is the discoverable half: a marker only inside an unopened
    // dropdown would need the reader to already be looking at it.
    await waitFor(() =>
      expect(
        within(screen.getByTestId("label")).getByRole("img", { name: /new options available/i })
      ).not.toBeNull()
    );

    // Radix drives the closed trigger from the selected option's children, so
    // the marker is mirrored there too. jsdom loads no stylesheet, so what is
    // observable is the class whose rule hides it —
    // `[data-slot=select-value] .…:hidden { display: none }`.
    const trigger = screen.getByRole("combobox", { name: /strategy/i });
    const mirrored = within(trigger).getByText("NEW");
    expect(mirrored.className).toContain("[[data-slot=select-value]_&]:hidden");

    await userEvent.click(trigger);
    const newOption = await screen.findByRole("option", { name: /structured_section/ });
    expect(within(newOption).getByText("NEW")).not.toBeNull();
    expect(screen.getByRole("option", { name: /recursive_splitter/ }).textContent).toBe(
      "recursive_splitter"
    );
  });
});
