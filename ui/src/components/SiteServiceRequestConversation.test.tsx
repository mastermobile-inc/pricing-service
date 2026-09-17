import "@testing-library/jest-dom/vitest";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { ApiSessionExpiredError, api } from "../api/client";
import toast from "react-hot-toast";
import { SiteServiceRequestConversation } from "./SiteServiceRequestConversation";

vi.mock("../api/client", async () => {
  const actual = await vi.importActual<typeof import("../api/client")>("../api/client");
  return {
    api: { get: vi.fn(), post: vi.fn() },
    ApiSessionExpiredError: actual.ApiSessionExpiredError,
  };
});

vi.mock("react-hot-toast", () => ({
  default: { success: vi.fn(), error: vi.fn() },
}));

const failedMessage = {
  id: "command:41",
  direction: "outbound",
  authorLabel: "Тимур Тибилов · Поддержка",
  text: "Ответ клиенту",
  createdAt: "2026-08-31T10:00:00+03:00",
  deliveryStatus: "failed",
  errorCode: "message_write_failed",
  retryable: true,
  visibleToCustomer: true,
  attachments: [],
};

function orderStatus(overrides: Record<string, unknown> = {}) {
  return {
    dealId: 33485,
    orderRef: "240315",
    tracking: "10311127882",
    statusText: "Вручен 26.08.2026 14:58",
    trackingLink: "https://www.cdek.ru/ru/tracking/?order_id=10311127882",
    plannedDeliveryDate: null,
    storageDate: null,
    multipleShipments: false,
    customerMessage: "Здравствуйте! По вашему заказу №240315: Вручен 26.08.2026 14:58.",
    ...overrides,
  };
}

/** Раскладывает ответы мока по адресу: переписка отдельно, статус заказа отдельно. */
function mockByUrl(options: {
  conversations: Array<Record<string, unknown>>;
  order?: Record<string, unknown> | null;
}) {
  const pages = [...options.conversations];
  vi.mocked(api.get).mockImplementation((url: string) => {
    if (url.endsWith("/order-status")) {
      return options.order
        ? Promise.resolve({ data: options.order })
        : Promise.reject(new Error("order status is unavailable"));
    }
    const page = pages.length > 1 ? pages.shift() : pages[0];
    return Promise.resolve({ data: page });
  });
}

function conversation(overrides: Record<string, unknown> = {}) {
  return {
    itemId: 392,
    sourceKind: "site_ticket",
    ticketId: 760,
    canReply: false,
    canAttachFiles: false,
    originalUrl: "https://master-mobile.ru/personal/tickets/?ID=760",
    nextBeforeId: null,
    messages: [failedMessage],
    ...overrides,
  };
}

describe("SiteServiceRequestConversation", () => {
  beforeAll(() => {
    Object.defineProperty(HTMLElement.prototype, "scrollTo", {
      configurable: true,
      value: vi.fn(),
    });
  });

  beforeEach(() => {
    vi.mocked(api.get).mockReset();
    vi.mocked(api.post).mockReset();
  });

  afterEach(cleanup);

  it("показывает понятный read-only режим до истории и скрывает все действия", async () => {
    vi.mocked(api.get).mockResolvedValue({ data: conversation() });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(await screen.findByText("Тикет сайта №760")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Только просмотр. Отправка ответов из карточки временно недоступна.",
    );
    expect(screen.getByRole("link", { name: "Открыть обращение на сайте" })).toHaveAttribute(
      "href",
      "https://master-mobile.ru/personal/tickets/?ID=760",
    );
    expect(screen.queryByText("Ответ попадёт в личный кабинет клиента.")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Повторить" })).not.toBeInTheDocument();
    expect(screen.queryByPlaceholderText("Напишите ответ клиенту")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Прикрепить файлы")).not.toBeInTheDocument();
  });

  it("разрешает текст и retry, но не предлагает файлы без capability", async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: conversation({ canReply: true, canAttachFiles: false }),
    });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(await screen.findByText("Ответ попадёт в личный кабинет клиента.")).toBeVisible();
    expect(screen.getByPlaceholderText("Напишите ответ клиенту")).toBeVisible();
    expect(screen.getByRole("button", { name: "Повторить" })).toBeVisible();
    expect(screen.queryByText("Только просмотр.")).not.toBeInTheDocument();
    expect(screen.queryByLabelText("Прикрепить файлы")).not.toBeInTheDocument();
  });

  it("показывает файлы только при отдельной capability", async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: conversation({ canReply: true, canAttachFiles: true }),
    });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(await screen.findByLabelText("Прикрепить файлы")).toBeVisible();
  });

  it("направляет email-обращение в штатный таймлайн CRM", async () => {
    vi.mocked(api.get).mockResolvedValue({
      data: conversation({
        sourceKind: "bitrix_mail",
        ticketId: null,
        originalUrl: null,
        messages: [],
      }),
    });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(await screen.findByText("Обращение по электронной почте")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent(
      "Ответьте через письмо в таймлайне CRM.",
    );
  });

  it("подгружает предыдущую страницу и сохраняет статусы внутренних заметок", async () => {
    const internalNote = {
      ...failedMessage,
      id: "note:17",
      direction: "internal",
      authorLabel: "Администратор · Поддержка",
      text: "Проверить заказ у логиста",
      deliveryStatus: "note",
      errorCode: null,
      retryable: false,
      visibleToCustomer: false,
    };
    mockByUrl({
      conversations: [
        conversation({ canReply: true, nextBeforeId: 41 }),
        conversation({
          canReply: true,
          nextBeforeId: null,
          messages: [internalNote],
        }),
      ],
      order: null,
    });

    render(<SiteServiceRequestConversation itemId={392} />);
    fireEvent.click(await screen.findByRole("button", { name: "Показать предыдущие сообщения" }));

    const noteText = await screen.findByText("Проверить заказ у логиста");
    expect(noteText).toBeVisible();
    expect(noteText.closest("article")).toHaveTextContent("Внутренняя заметка");
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith(
        "/site-service-requests/ui/items/392/conversation",
        { params: { beforeId: 41 } },
      ),
    );
  });

  it("предлагает готовый ответ «Где заказ» со статусом доставки", async () => {
    mockByUrl({
      conversations: [conversation({ canReply: true })],
      order: orderStatus(),
    });

    render(<SiteServiceRequestConversation itemId={392} />);

    const select = await screen.findByLabelText("Шаблон ответа");
    const option = await screen.findByRole("option", { name: "Где заказ — статус доставки" });
    fireEvent.change(select, { target: { value: (option as HTMLOptionElement).value } });

    expect(screen.getByPlaceholderText("Напишите ответ клиенту")).toHaveValue(
      "Здравствуйте! По вашему заказу №240315: Вручен 26.08.2026 14:58.",
    );
  });

  it("не подставляет общий трек, когда по заказу несколько отправлений", async () => {
    mockByUrl({
      conversations: [conversation({ canReply: true })],
      order: orderStatus({ multipleShipments: true, customerMessage: "" }),
    });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(
      await screen.findByText(
        "По заказу несколько отправлений — общий трек клиенту отправлять нельзя. Проверьте отправления в сделке.",
      ),
    ).toBeVisible();
    expect(
      screen.queryByRole("option", { name: "Где заказ — статус доставки" }),
    ).not.toBeInTheDocument();
  });

  it("молчит, когда статус заказа недоступен", async () => {
    mockByUrl({ conversations: [conversation({ canReply: true })], order: null });

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(await screen.findByLabelText("Шаблон ответа")).toBeVisible();
    expect(
      screen.queryByRole("option", { name: "Где заказ — статус доставки" }),
    ).not.toBeInTheDocument();
  });

  it("говорит обновить страницу, когда сессия вкладки истекла", async () => {
    vi.mocked(api.get).mockRejectedValue(new ApiSessionExpiredError());

    render(<SiteServiceRequestConversation itemId={392} />);

    expect(
      await screen.findByText("Сессия вкладки истекла. Обновите страницу (F5), чтобы продолжить."),
    ).toBeVisible();
  });

  it("не выдаёт «Не удалось отправить ответ», когда сессия истекла", async () => {
    mockByUrl({ conversations: [conversation({ canReply: true })], order: null });
    vi.mocked(api.post).mockRejectedValue(new ApiSessionExpiredError());

    render(<SiteServiceRequestConversation itemId={392} />);
    fireEvent.change(await screen.findByPlaceholderText("Напишите ответ клиенту"), {
      target: { value: "Заказ уже в пути" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Отправить клиенту" }));

    await waitFor(() =>
      expect(toast.error).toHaveBeenCalledWith(
        "Сессия вкладки истекла. Обновите страницу (F5), чтобы продолжить.",
      ),
    );
    expect(toast.error).not.toHaveBeenCalledWith("Не удалось отправить ответ");
  });
});
