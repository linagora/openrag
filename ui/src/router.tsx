import { lazy, Suspense } from "react";
import { createBrowserRouter, Navigate } from "react-router-dom";
import { AdminLayout } from "@/components/layout/admin-layout";
import { AdminRoute } from "@/components/layout/admin-route";
import LoginPage from "@/pages/login";
import OverviewPage from "@/pages/admin/overview";
import PartitionListPage from "@/pages/admin/partitions/list";
import PartitionDetailPage from "@/pages/admin/partitions/detail";
import DocumentListPage from "@/pages/admin/documents/list";
import DocumentDetailPage from "@/pages/admin/documents/detail";
import JobListPage from "@/pages/admin/jobs/list";
import JobDetailPage from "@/pages/admin/jobs/detail";
import { ProtectedRoute } from "@/components/layout/protected-route";

// Admin-only pages — lazy loaded so they are never downloaded by regular users
const ModelsPage = lazy(() => import("@/pages/admin/models"));
const PresetsPage = lazy(() => import("@/pages/admin/presets"));
const UserListPage = lazy(() => import("@/pages/admin/users/list"));
const UserDetailPage = lazy(() => import("@/pages/admin/users/detail"));
const SystemPage = lazy(() => import("@/pages/admin/system"));
const SettingsPage = lazy(() => import("@/pages/app/settings"));

const basename = import.meta.env.BASE_URL.replace(/\/+$/, "") || "/";

export const router = createBrowserRouter(
  [
    {
      path: "/login",
      element: <LoginPage />,
    },
    {
      path: "/",
      element: (
        <ProtectedRoute>
          <AdminLayout />
        </ProtectedRoute>
      ),
      children: [
        // Shared routes — statically imported, available to all authenticated users
        { index: true, element: <OverviewPage /> },
        { path: "partitions", element: <PartitionListPage /> },
        { path: "partitions/:name", element: <PartitionDetailPage /> },
        { path: "documents", element: <DocumentListPage /> },
        { path: "documents/:partition/:fileId", element: <DocumentDetailPage /> },
        { path: "jobs", element: <JobListPage /> },
        { path: "jobs/:id", element: <JobDetailPage /> },
        {
          path: "settings",
          element: (
            <Suspense fallback={<div className="p-6">Loading...</div>}>
              <SettingsPage />
            </Suspense>
          ),
        },
        // Admin-only routes — lazy loaded, role-guarded
        {
          path: "models",
          element: (
            <AdminRoute>
              <Suspense fallback={<div className="p-6">Loading...</div>}>
                <ModelsPage />
              </Suspense>
            </AdminRoute>
          ),
        },
        {
          path: "presets",
          element: (
            <AdminRoute>
              <Suspense fallback={<div className="p-6">Loading...</div>}>
                <PresetsPage />
              </Suspense>
            </AdminRoute>
          ),
        },
        {
          path: "users",
          element: (
            <AdminRoute>
              <Suspense fallback={<div className="p-6">Loading...</div>}>
                <UserListPage />
              </Suspense>
            </AdminRoute>
          ),
        },
        {
          path: "users/:id",
          element: (
            <AdminRoute>
              <Suspense fallback={<div className="p-6">Loading...</div>}>
                <UserDetailPage />
              </Suspense>
            </AdminRoute>
          ),
        },
        {
          path: "system",
          element: (
            <AdminRoute>
              <Suspense fallback={<div className="p-6">Loading...</div>}>
                <SystemPage />
              </Suspense>
            </AdminRoute>
          ),
        },
      ],
    },
    {
      path: "*",
      element: <Navigate to="/" replace />,
    },
  ],
  { basename },
);
